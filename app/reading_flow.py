"""Flow reading — an endless, LLM-generated reading session on a topic the
learner picks.

The learner just scrolls. They tap words they don't know as they meet them; that
is the only signal. From it we hold a running estimate of how big their reading
vocabulary is (`rank_est`, a frequency-rank cutoff) and steer each next chunk's
difficulty to keep the unknown-word rate near ~3% — the comprehensible-input
sweet spot. Words the learner already has SRS cards for are quietly seeded into
the text for extra in-context reps.

Store + orchestration here; the prompt lives in llm.reading_flow_chunk.
"""
import json  # noqa: F401
import os
import re
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import accent      # noqa: E402
import db          # noqa: E402  (root module)
import llm         # noqa: E402
import proficiency  # noqa: E402
import srs         # noqa: E402,F401  (kept for parity / future use)
import store       # noqa: E402
import tts_hq      # noqa: E402
import ytdlp       # noqa: E402  (MEDIA_DIR)

_AUDIO_DIR = os.path.join(ytdlp.MEDIA_DIR, "reading-audio")

TARGET_UNKNOWN = 0.02          # aim: ~98% of running words already known
                              # (Hu & Nation 2000 / Nation 2006 — the coverage
                              # at which unassisted reading comprehension holds)
_RANK_MIN, _RANK_MAX = 800, 22000
_WORD = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё-]*")


def _c():
    return store.connect()


def _join(v):
    if isinstance(v, (list, tuple)):
        return "\n\n".join(str(p).strip() for p in v if str(p).strip())
    return str(v or "").strip()


# ---------------------------------------------------------------- vocab model

def _known_set(c):
    """Lemmas the reader plainly knows — everything they have a card for, plus
    the word-formation families of those. Cheap; recomputed per generation."""
    known = {r["normalized_text"] for r in c.execute(
        "SELECT DISTINCT normalized_text FROM srs_cards WHERE is_phrase=0")}
    try:
        known |= {r["lemma"] for r in c.execute(
            """SELECT wf.lemma FROM word_family wf WHERE wf.root IN (
                 SELECT w2.root FROM word_family w2
                 JOIN srs_cards s ON s.normalized_text = w2.lemma AND s.is_phrase=0)""")}
    except Exception:  # noqa: BLE001
        pass
    return known


def _content_lemmas(text):
    """{lemma} for the Cyrillic word tokens in `text` (1-char tokens dropped)."""
    out = {}
    for m in _WORD.finditer(text or ""):
        w = m.group(0)
        if len(w) < 2:
            continue
        out.setdefault(db.lemma_key(w), w)
    return out


def _predict_unknown(text, rank_est, known, ranks=None):
    lemmas = set(_content_lemmas(text))
    if not lemmas:
        return 0.0, []
    ranks = ranks if ranks is not None else store.rank_map(lemmas)
    unknown = [l for l in lemmas
               if l not in known and (l not in ranks or ranks[l] > rank_est)]
    return len(unknown) / len(lemmas), unknown


def _seed_words(c, limit=12):
    """Single-word card lemmas that would most benefit from another in-context
    exposure: recently started, lapsing, or still shaky — NOT the mature cards
    that are already sticking. Ranked by how much reinforcement they need."""
    rows = c.execute(
        """SELECT normalized_text, front_word,
                  ( COALESCE(lapses,0) * 4
                    + (CASE WHEN fsrs_state IN (1,3) THEN 3 ELSE 0 END)
                    + (CASE WHEN created_at >= datetime('now','-21 days') THEN 3 ELSE 0 END)
                    + (CASE WHEN created_at >= datetime('now','-45 days') THEN 1 ELSE 0 END)
                    + (CASE WHEN due <= datetime('now','+2 days') THEN 2 ELSE 0 END)
                    - (COALESCE(stability,0) / 8.0) ) AS need
           FROM srs_cards
           WHERE is_phrase=0 AND suspended=0
             AND (stability IS NULL OR stability < 21)      -- young / not yet mature
             AND ( last_review IS NOT NULL                  -- already introduced …
                   OR created_at >= datetime('now','-14 days') )   -- … or brand new
           ORDER BY need DESC, due ASC
           LIMIT ?""", (limit,)).fetchall()
    return [(r["front_word"] or r["normalized_text"]).replace("́", "") for r in rows]


# ---------------------------------------------------------------- sessions

def suggested_topics():
    return [
        "A day in the life of a programmer in New York",
        "How my girlfriend and I met",
        "Two friends argue about whether AI will take everyone's jobs",
        "A short mystery: something is missing from the apartment",
        "Cooking borscht for the first time — what went wrong",
        "Letters home from someone who just moved to Moscow",
        "The most useful Russian slang, explained with examples",
        "A gentle sci-fi story about a city that never sleeps",
        "How Russians actually celebrate New Year",
        "Small talk that goes surprisingly deep on a long train ride",
    ]


# ---------------------------------------------------------------- topic pool

# The subject picker's per-category topic lists evolve: a topic you start a piece
# from is retired and quietly replaced, and "refresh" swaps the whole set.
_TOPICS_PER_DOMAIN = 5
_SEEN_CAP = 60


def _topic_state(key):
    try:
        raw = srs.get_setting(key, "") or ""
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_topic_state(key, st):
    srs.set_setting(key, json.dumps(st, ensure_ascii=False))


def _seed_topics(did):
    for d in proficiency.DOMAINS:
        if d["id"] == did:
            return list(d.get("topics") or [])
    return []


def domain_topics(did):
    """The current suggestions for a category — the stored pool if we have one,
    else the static seed minus anything already used."""
    pool = _topic_state("reading_topic_pool")
    cur = pool.get(did)
    if cur:
        return cur[:_TOPICS_PER_DOMAIN]
    seen = set(_topic_state("reading_topic_used").get(did, []))
    fresh = [t for t in _seed_topics(did) if t not in seen]
    return fresh[:_TOPICS_PER_DOMAIN] or _seed_topics(did)[:_TOPICS_PER_DOMAIN]


def picker_domains():
    """proficiency.domains_public() with the live, evolving topic lists."""
    out = proficiency.domains_public()
    for d in out:
        d["topics"] = domain_topics(d["id"])
    return out


def _domain_meta(did):
    for d in proficiency.domains_public():
        if d["id"] == did:
            return d
    return {"label": did, "blurb": "", "form": "", "grounded": False}


def _gen_topics(did, n, avoid):
    m = _domain_meta(did)
    try:
        return llm.reading_topics(m.get("label", did), m.get("blurb", ""),
                                  m.get("form", ""), m.get("grounded", False),
                                  avoid=avoid, n=n)
    except Exception:  # noqa: BLE001
        return []


def note_topic_started(did, topic):
    """A piece was started from this listed suggestion — retire it, mark it seen,
    and top the category back up with a fresh idea (in the background)."""
    topic = (topic or "").strip()
    if not topic or not did:
        return
    pool = _topic_state("reading_topic_pool")
    cur = pool.get(did) or domain_topics(did)
    if topic not in cur and topic not in _seed_topics(did):
        return                                  # a free-typed prompt, not a suggestion
    used = _topic_state("reading_topic_used")
    seen = used.get(did, [])
    if topic not in seen:
        seen.append(topic)
    used[did] = seen[-_SEEN_CAP:]
    _save_topic_state("reading_topic_used", used)
    pool[did] = [t for t in cur if t != topic]
    _save_topic_state("reading_topic_pool", pool)
    threading.Thread(target=_rotate_one, args=(did,), daemon=True).start()


def _rotate_one(did):
    used = _topic_state("reading_topic_used").get(did, [])
    pool = _topic_state("reading_topic_pool")
    cur = pool.get(did) or []
    if len(cur) >= _TOPICS_PER_DOMAIN:
        return
    new = _gen_topics(did, _TOPICS_PER_DOMAIN - len(cur), avoid=cur + used)
    if not new:
        return
    pool = _topic_state("reading_topic_pool")          # re-read (thread)
    cur = pool.get(did) or []
    have = set(cur)
    cur += [t for t in new if t not in have]
    pool[did] = cur[:_TOPICS_PER_DOMAIN]
    _save_topic_state("reading_topic_pool", pool)


def refresh_topics(did):
    """Replace a category's whole suggestion list with fresh ideas (the user hit
    'more ideas'). Synchronous — the caller shows a spinner."""
    used = _topic_state("reading_topic_used").get(did, [])
    pool = _topic_state("reading_topic_pool")
    cur = pool.get(did) or domain_topics(did)
    new = _gen_topics(did, _TOPICS_PER_DOMAIN, avoid=cur + used + _seed_topics(did))
    if not new:
        return cur
    pool[did] = new[:_TOPICS_PER_DOMAIN]
    _save_topic_state("reading_topic_pool", pool)
    return pool[did]


PARTS = 5                       # a piece is this many parts, then it ends


def _plan_dict(s):
    try:
        return json.loads(s["plan"]) if s["plan"] else None
    except Exception:  # noqa: BLE001
        return None


def create(topic="", prompt="", domain=""):
    topic = (topic or "").strip()
    prompt = (prompt or "").strip()
    domain = (domain or "").strip() or proficiency.classify(topic, prompt)
    if topic:
        try:
            note_topic_started(domain, topic)
        except Exception:  # noqa: BLE001
            pass
    start = proficiency.starting_rank(domain)
    cefr = _level_label(start)
    try:
        plan = llm.reading_flow_plan(
            topic, prompt, style=proficiency.domain_style(domain),
            grounding=proficiency.domain_grounding(domain), cefr=cefr, parts=PARTS)
    except Exception:  # noqa: BLE001
        plan = None
    label = (plan.get("title") if plan else "") or topic or (prompt[:60] if prompt else "Free reading")
    c = _c()
    cur = c.execute(
        "INSERT INTO reading_flow_sessions(topic, prompt, domain, rank_est, plan, total_parts) "
        "VALUES(?,?,?,?,?,?)",
        (label, prompt or None, domain, start,
         json.dumps(plan, ensure_ascii=False) if plan else None, PARTS))
    sid = cur.lastrowid
    c.commit()
    c.close()
    _generate(sid)
    return sid


def sequel(sid):
    """A linked follow-up piece — same world, moved forward, opening on a twist."""
    c = _c()
    p = c.execute("SELECT * FROM reading_flow_sessions WHERE id=?", (sid,)).fetchone()
    c.close()
    if not p:
        return None
    base = _plan_dict(p)
    prev_title = (base.get("title") if base else None) or p["topic"] or "the last piece"
    domain = p["domain"]
    start = p["rank_est"] or proficiency.starting_rank(domain)
    try:
        plan = llm.reading_flow_plan(
            p["topic"] or prev_title, p["prompt"] or "",
            style=proficiency.domain_style(domain),
            grounding=proficiency.domain_grounding(domain),
            cefr=_level_label(start), parts=PARTS,
            sequel_of={"title": prev_title, "summary": (p["summary"] or "")[:400]})
    except Exception:  # noqa: BLE001
        plan = None
    label = (plan.get("title") if plan else "") or (prev_title + " — продолжение")
    c = _c()
    cur = c.execute(
        "INSERT INTO reading_flow_sessions(topic, prompt, domain, rank_est, plan, total_parts, parent_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (label, p["prompt"], domain, start,
         json.dumps(plan, ensure_ascii=False) if plan else None, PARTS, sid))
    new_sid = cur.lastrowid
    c.commit()
    c.close()
    _generate(new_sid)
    return new_sid


def _generate(sid):
    """Make the next chunk for a session — tune rank_est from taps so far, call
    the model, pre-check the difficulty (regenerate once if it's way off), store."""
    c = _c()
    s = c.execute("SELECT * FROM reading_flow_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        return
    total = s["total_parts"] or PARTS
    seq = (s["chunks"] or 0) + 1
    if seq > total:                       # every part already written — nothing to add
        c.close()
        return
    rank_est = _retune(c, s)
    known = _known_set(c)
    seeds = _seed_words(c)
    plan = _plan_dict(s)
    c.close()
    grounding = proficiency.domain_grounding(s["domain"])
    style = proficiency.domain_style(s["domain"])
    target = []
    if s["domain"] == "fiction":
        try:
            # ease the reader in: part 1 carries one target word, later parts a
            # few more — and the SAME early words recur (the slice grows from the
            # front) so each gets several exposures instead of a one-off dump.
            pool = proficiency.fiction_target_words(limit=10)
            n_t = (1, 2, 2, 3, 3)[min(max(seq, 1), 5) - 1] if total >= 4 else 2
            target = pool[:n_t]
        except Exception:  # noqa: BLE001
            target = []
        if total > 1:
            style = style + (
                f"\n\nPACING — this is part {seq} of {total} of easing the reader "
                "into 19th-century prose. "
                + ("Opening part: stay close to vocabulary the reader already "
                   "knows; at most one or two new period words, each obvious from "
                   "context. Set the scene plainly."
                   if seq <= 2 else
                   "Reuse the period words already introduced in earlier parts "
                   "(see SO FAR); add at most one or two new ones. Richer texture "
                   "is welcome now, but never a wall of unfamiliar words."))

    text, summary, pred = "", s["summary"], None
    for attempt in (1, 2):
        try:
            hint = rank_est if attempt == 1 else max(_RANK_MIN, int(rank_est * 0.55))
            d = llm.reading_flow_chunk(s["topic"], s["prompt"], s["summary"],
                                       hint, seeds if attempt == 1 else (),
                                       grounding=grounding, style=style,
                                       plan=plan, part=seq, total=total,
                                       target_words=target if attempt == 1 else ())
            text = _join(d.get("text"))
            summary = (d.get("summary") or s["summary"] or "").strip()[:600]
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                _set(sid, status="error", error=str(e)[:300])
                return
            continue
        pred, _ = _predict_unknown(text, rank_est, known)
        if pred <= TARGET_UNKNOWN * 1.8 or attempt == 2:
            break  # good enough, or out of retries — serve it

    nwords = len(_WORD.findall(text))
    try:
        acc = accent.accent_paragraphs(
            [p for p in re.split(r"\n\s*\n", text) if p.strip()], passes=2)
        acc_text = "\n\n".join(acc) if acc else None
    except Exception:  # noqa: BLE001
        acc_text = None
    c = _c()
    c.execute(
        "INSERT INTO reading_flow_chunks(session_id, seq, text, text_accented, rank_est, n_words, pred_unknown) "
        "VALUES(?,?,?,?,?,?,?)", (sid, seq, text, acc_text, rank_est, nwords, pred))
    c.execute(
        "UPDATE reading_flow_sessions SET chunks=?, summary=?, status='active', error=NULL "
        "WHERE id=?", (seq, summary, sid))
    c.commit()
    c.close()


def _retune(c, s):
    """Re-estimate rank_est from the taps-per-100-words over the chunks read so
    far. Too many taps -> the reader knows fewer words than we thought."""
    rank_est = s["rank_est"]
    read = c.execute(
        "SELECT COALESCE(SUM(n_words),0) w FROM reading_flow_chunks "
        "WHERE session_id=? AND read=1", (s["id"],)).fetchone()["w"]
    if read < 60:
        return rank_est
    taps = c.execute(
        "SELECT COUNT(*) n FROM reading_flow_unknown WHERE session_id=?",
        (s["id"],)).fetchone()["n"]
    per100 = taps / read * 100
    # a reader taps only some of the words they don't know, so a ~2% unknown
    # target sits near ~1.5 taps / 100 running words
    if per100 > 6:
        rank_est -= 1400
    elif per100 > 3.5:
        rank_est -= 600
    elif per100 > 2:
        rank_est -= 250
    elif taps == 0:
        # zero taps is ambiguous — could be effortless comprehension or could be
        # skimming past unknown words. Not enough signal to raise the level;
        # hold steady until the reader actually engages by tapping.
        pass
    elif per100 < 0.7:
        rank_est += 450
    elif per100 < 1.3:
        rank_est += 150
    # any tapped word that's actually common is strong evidence we're too high
    common_taps = c.execute(
        "SELECT COUNT(*) n FROM reading_flow_unknown WHERE session_id=? AND rank IS NOT NULL AND rank < ?",
        (s["id"], rank_est)).fetchone()["n"]
    rank_est -= 120 * common_taps
    rank_est = max(_RANK_MIN, min(_RANK_MAX, rank_est))
    if rank_est != s["rank_est"]:
        c.execute("UPDATE reading_flow_sessions SET rank_est=? WHERE id=?", (rank_est, s["id"]))
        c.commit()
    return rank_est


def next_chunk(sid, read_seq=None, read_words=0):
    c = _c()
    s = c.execute("SELECT chunks, total_parts FROM reading_flow_sessions WHERE id=?",
                  (sid,)).fetchone()
    if read_seq:
        c.execute(
            "UPDATE reading_flow_chunks SET read=1, n_words=MAX(n_words,?) "
            "WHERE session_id=? AND seq=?", (int(read_words or 0), sid, int(read_seq)))
        done = s and int(read_seq) >= (s["total_parts"] or PARTS)
        c.execute(
            "UPDATE reading_flow_sessions SET words_read = ("
            "  SELECT COALESCE(SUM(n_words),0) FROM reading_flow_chunks WHERE session_id=? AND read=1), "
            "  last_read_at=datetime('now'), "
            "  status=CASE WHEN status='archived' THEN 'archived' ELSE ? END WHERE id=?",
            (sid, "done" if done else "active", sid))
        c.commit()
    c.close()
    try:
        proficiency.note_session(sid)
    except Exception:  # noqa: BLE001
        pass
    if not s or (s["chunks"] or 0) < (s["total_parts"] or PARTS):
        _generate(sid)
    return _chunk(sid, "last")


def tap(sid, surface, sentence="", chunk_seq=None):
    surface = (surface or "").strip()
    sentence = (sentence or "").strip()
    lemma = db.lemma_key(surface)
    if not lemma:
        return {"lemma": "", "gloss": None}
    rank = store.rank_map({lemma}).get(lemma)
    c = _c()
    c.execute(
        """INSERT INTO reading_flow_unknown(session_id, lemma, surface, sentence, chunk_seq, rank)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(session_id, lemma) DO UPDATE SET
             surface=excluded.surface, sentence=excluded.sentence, at=datetime('now')""",
        (sid, lemma, surface, sentence[:400], chunk_seq, rank))
    c.execute("UPDATE reading_flow_sessions SET unknown_seen = "
              "(SELECT COUNT(*) FROM reading_flow_unknown WHERE session_id=?) WHERE id=?",
              (sid, sid))
    c.commit()
    c.close()
    return {"lemma": lemma, "rank": rank, **_word_gloss(lemma, surface, sentence)}


def _word_gloss(lemma, surface, sentence=""):
    """A translation for a tapped word: cache -> local dictionary -> a live
    one-word LLM gloss in context (cached for next time). Always returns a dict
    with `gloss` and (when known) `accented`."""
    cached = store.word_gloss_get(lemma)
    if cached:
        return {"gloss": cached, "accented": store.accent_for(lemma)}
    quick = store.gloss_for(surface) or store.gloss_for(lemma)
    if quick:
        return {"gloss": quick, "accented": store.accent_for(lemma)}
    try:
        g = llm.translate_span(sentence or surface, surface)
    except Exception:  # noqa: BLE001
        return {"gloss": None, "accented": None}
    gloss = (g.get("translation") or "").strip()
    if gloss:
        store.word_gloss_set(lemma, gloss)
    return {"gloss": gloss or None,
            "accented": (g.get("stressed") or g.get("dict_form") or "").strip() or None}


def untap(sid, lemma):
    c = _c()
    c.execute("DELETE FROM reading_flow_unknown WHERE session_id=? AND lemma=?",
              (sid, db.lemma_key(lemma)))
    c.execute("UPDATE reading_flow_sessions SET unknown_seen = "
              "(SELECT COUNT(*) FROM reading_flow_unknown WHERE session_id=?) WHERE id=?",
              (sid, sid))
    c.commit()
    c.close()
    return True


def _set(sid, **cols):
    if not cols:
        return
    c = _c()
    c.execute(f"UPDATE reading_flow_sessions SET {','.join(k+'=?' for k in cols)} WHERE id=?",
              (*cols.values(), sid))
    c.commit()
    c.close()


# ---------------------------------------------------------------- read

_CEFR = ["a2", "a2", "b1", "b1", "b2", "b2", "c1"]


def _level_label(rank_est):
    cefr, _ = llm._reading_level_line(rank_est)
    return cefr


def _chunk(sid, which="last"):
    c = _c()
    if which == "last":
        r = c.execute("SELECT * FROM reading_flow_chunks WHERE session_id=? "
                      "ORDER BY seq DESC LIMIT 1", (sid,)).fetchone()
    else:
        r = c.execute("SELECT * FROM reading_flow_chunks WHERE session_id=? AND seq=?",
                      (sid, int(which))).fetchone()
    s = c.execute("SELECT status, error, rank_est, total_parts, chunks, plan "
                  "FROM reading_flow_sessions WHERE id=?", (sid,)).fetchone()
    c.close()
    if not s:
        return None
    if s["status"] == "error":
        return {"error": s["error"] or "generation failed"}
    if not r:
        return {"error": "no chunk"}
    total = s["total_parts"] or PARTS
    plan = _plan_dict(s)
    return {"seq": r["seq"], "text": r["text_accented"] or r["text"],
            "plain": r["text"], "n_words": r["n_words"],
            "level": _level_label(s["rank_est"]), "rank_est": s["rank_est"],
            "part": r["seq"], "total": total, "title": (plan or {}).get("title"),
            "done": s["status"] == "done" or (s["chunks"] or 0) >= total}


def session(sid):
    c = _c()
    s = c.execute("SELECT * FROM reading_flow_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        return None
    chunks = [{"seq": r["seq"], "text": r["text_accented"] or r["text"],
               "plain": r["text"], "n_words": r["n_words"],
               "has_audio": bool(r["audio_path"])}
              for r in c.execute(
                  "SELECT * FROM reading_flow_chunks WHERE session_id=? ORDER BY seq", (sid,))]
    unknown = [{"lemma": r["lemma"], "surface": r["surface"], "sentence": r["sentence"],
                "rank": r["rank"], "carded": bool(r["carded"]),
                "gloss": store.word_gloss_get(r["lemma"]) or store.gloss_for(r["surface"])
                         or store.gloss_for(r["lemma"])}
               for r in c.execute(
                   "SELECT * FROM reading_flow_unknown WHERE session_id=? ORDER BY at", (sid,))]
    c.close()
    plan = _plan_dict(s)
    total = s["total_parts"] or PARTS
    return {"id": s["id"], "topic": s["topic"], "prompt": s["prompt"],
            "domain": s["domain"], "domain_label": proficiency.domain_label(s["domain"]),
            "status": s["status"], "error": s["error"],
            "title": (plan or {}).get("title") or s["topic"],
            "hook": (plan or {}).get("hook"),
            "total": total, "parts_made": s["chunks"] or 0,
            "done": s["status"] == "done" or (s["chunks"] or 0) >= total,
            "parent_id": s["parent_id"],
            "level": _level_label(s["rank_est"]), "rank_est": s["rank_est"],
            "words_read": s["words_read"], "unknown_seen": s["unknown_seen"],
            "created_at": s["created_at"], "last_read_at": s["last_read_at"],
            "chunks": chunks, "unknown": unknown}


def chunk_audio(sid, seq):
    """Path to the spoken version of one chunk — synthesised on first request
    (local Silero, with the dictionary stress) and cached. None on failure."""
    c = _c()
    r = c.execute(
        "SELECT text, text_accented, audio_path FROM reading_flow_chunks "
        "WHERE session_id=? AND seq=?", (sid, int(seq))).fetchone()
    c.close()
    if not r:
        return None
    if r["audio_path"] and os.path.exists(r["audio_path"]):
        return r["audio_path"]
    src = (r["text_accented"] or r["text"] or "").strip()   # keep the stress marks
    if not src:
        return None
    os.makedirs(_AUDIO_DIR, exist_ok=True)
    out = os.path.join(_AUDIO_DIR, f"read-{sid}-{seq}.m4a")
    try:
        tts_hq.synth_to_file(src, out)          # auto: Apple `say`, then Silero
    except Exception as e:  # noqa: BLE001
        print(f"[reading] tts {sid}/{seq}: {e}", flush=True)
        return None
    c = _c()
    c.execute("UPDATE reading_flow_chunks SET audio_path=? WHERE session_id=? AND seq=?",
              (out, sid, int(seq)))
    c.commit()
    c.close()
    return out


def set_archived(sid, on=True):
    """Move a finished piece to the archive (kept as a reference, out of the way)
    or bring it back."""
    c = _c()
    if on:
        c.execute("UPDATE reading_flow_sessions SET status='archived' WHERE id=?", (sid,))
    else:
        c.execute(
            "UPDATE reading_flow_sessions SET status="
            "CASE WHEN chunks >= COALESCE(total_parts,?) THEN 'done' ELSE 'active' END "
            "WHERE id=?", (PARTS, sid))
    c.commit()
    c.close()
    return True


def recent(limit=60, archived=False):
    c = _c()
    where = "status = 'archived'" if archived else "status IS NOT 'archived'"
    rows = c.execute(
        "SELECT id, topic, prompt, domain, status, chunks, total_parts, parent_id, "
        "words_read, unknown_seen, rank_est, created_at, last_read_at "
        f"FROM reading_flow_sessions WHERE {where} "
        "ORDER BY COALESCE(last_read_at, created_at) DESC LIMIT ?",
        (limit,)).fetchall()
    c.close()
    return [{"id": r["id"], "topic": r["topic"], "status": r["status"],
             "archived": r["status"] == "archived",
             "domain": r["domain"], "domain_label": proficiency.domain_label(r["domain"]),
             "level": _level_label(r["rank_est"]), "chunks": r["chunks"],
             "total": r["total_parts"] or PARTS, "parent_id": r["parent_id"],
             "done": r["status"] in ("done", "archived") or (r["chunks"] or 0) >= (r["total_parts"] or PARTS),
             "words_read": r["words_read"], "unknown_seen": r["unknown_seen"],
             "created_at": r["created_at"], "last_read_at": r["last_read_at"]}
            for r in rows]


def mark_carded(sid, lemmas):
    c = _c()
    for lem in lemmas:
        c.execute("UPDATE reading_flow_unknown SET carded=1 WHERE session_id=? AND lemma=?",
                  (sid, db.lemma_key(lem)))
    c.commit()
    c.close()


def delete(sid):
    c = _c()
    for r in c.execute("SELECT audio_path FROM reading_flow_chunks WHERE session_id=?", (sid,)):
        if r["audio_path"] and os.path.exists(r["audio_path"]):
            try:
                os.remove(r["audio_path"])
            except OSError:
                pass
    for t in ("reading_flow_unknown", "reading_flow_chunks", "reading_flow_sessions"):
        col = "id" if t.endswith("sessions") else "session_id"
        c.execute(f"DELETE FROM {t} WHERE {col}=?", (sid,))
    c.commit()
    c.close()
    return True
