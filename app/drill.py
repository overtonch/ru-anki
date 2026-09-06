"""Grammar drill — an endless, self-graded flip-through that walks a syllabus of
Russian grammar concepts (see `grammar.py`) while the WORD difficulty moves with a
frequency band.

Each generated card tests one catalog concept (its id is stored in
`drill_items.skill`) with vocabulary from the current band, so progress can be
sliced by rule, by band, or by both. Cards are LLM-generated in rolling
background batches; nothing is FSRS-scheduled. A right streak bumps the band up
(rarer words), misses drop it. Any card can be promoted to a production card.
Wrong answers file a lapse and come back as spaced re-tests; from the grammar
page the learner can queue a burst of practice on any single concept.

Store + orchestration here; the LLM prompt is in llm.py, endpoints in main.py.
"""
import json
import threading

import grammar
import llm
import srs
import store

# frequency-rank bands, commonest first. Overlap so a band change isn't a cliff.
BANDS = [
    (7, 220),        # 0 — the absolute core (function words filtered out on sample)
    (170, 500),      # 1 — very common
    (450, 1100),     # 2 — common
    (1000, 2400),    # 3
    (2200, 5000),    # 4
    (4500, 12000),   # 5 — getting into the long tail
]
_BATCH = 14             # cards per LLM call (= concepts per batch)
_MIN_BUFFER = 14        # start generating ahead once fewer servable than this
_TARGET_BUFFER = 34     # ...and keep going until at least this many are queued
_BRIDGE = 6             # on a band change, keep this many current cards as a bridge
_UP_STREAK = 5          # this many right in a row → next band
_DOWN_STREAK = 2        # this many wrong in a row → previous band

# concept category → a coarse `kind` for the card label + the aspect-pair check
_CAT_KIND = {"aspect": "aspect", "government": "government", "motion": "motion",
             "genitive": "case", "accusative": "case", "dative": "case",
             "instrumental": "case", "prepositional": "case"}


def _kind_for(cc):
    return _CAT_KIND.get(cc.cat, cc.cat) if cc else None


def _grammar_level():
    v = str(srs.get_setting("drill_grammar_level", "b1")).lower()
    return v if v in grammar.LEVELS else "b1"


_topup_lock = threading.Lock()


def _c():
    return store.connect()


# ------------------------------------------------------------------ band / streak

def _band():
    return max(0, min(len(BANDS) - 1, int(srs.get_setting("drill_band", 1))))


# a card counts toward the buffer if it's ungraded, not a re-test, not a
# concept-focus burst, and either never served or served >30 min ago (a session
# abandoned mid-card)
_SERVABLE = ("verdict IS NULL AND retest_for IS NULL AND COALESCE(focus,0)=0 "
             "AND (served_at IS NULL OR served_at < datetime('now','-30 minutes'))")


def _servable_count(c):
    return c.execute(f"SELECT COUNT(*) n FROM drill_items WHERE {_SERVABLE}").fetchone()["n"]


def state():
    lo, hi = BANDS[_band()]
    c = _c()
    unseen = _servable_count(c)
    seen = c.execute("SELECT COUNT(*) n FROM drill_items WHERE verdict IS NOT NULL").fetchone()["n"]
    right = c.execute("SELECT COUNT(*) n FROM drill_items "
                      "WHERE verdict IN ('right','easy')").fetchone()["n"]
    lapses = c.execute("SELECT COUNT(*) n FROM drill_lapse WHERE resolved=0").fetchone()["n"]
    c.close()
    return {
        "band": _band(), "band_count": len(BANDS),
        "band_lo": lo, "band_hi": hi,
        "right_streak": int(srs.get_setting("drill_right", 0)),
        "wrong_streak": int(srs.get_setting("drill_wrong", 0)),
        "buffer": unseen, "seen": seen, "right": right, "open_lapses": lapses,
    }


def _apply_verdict(verdict):
    """Update streaks and, on a streak boundary, the band. Returns True if the
    band moved (caller should refresh the buffer)."""
    r = int(srs.get_setting("drill_right", 0))
    w = int(srs.get_setting("drill_wrong", 0))
    band = _band()
    moved = False
    if verdict == "right":
        r, w = r + 1, 0
        if r >= _UP_STREAK and band < len(BANDS) - 1:
            band, r, moved = band + 1, 0, True
    else:
        w, r = w + 1, 0
        if w >= _DOWN_STREAK and band > 0:
            band, w, moved = band - 1, 0, True
    srs.set_setting("drill_right", r)
    srs.set_setting("drill_wrong", w)
    if moved:
        srs.set_setting("drill_band", band)
    return moved


# ------------------------------------------------------------------ items

def _recent_lemmas(limit=60):
    c = _c()
    rows = c.execute(
        "SELECT DISTINCT lemma FROM drill_items WHERE lemma IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [r["lemma"] for r in rows]


# ------------------------------------------------------------- lapses / re-tests
#
# Every wrong answer files a lapse keyed by (lemma, skill). A few cards later it
# comes back as a re-test (first a copy of the exact card; on repeat misses,
# freshly generated cards drilling that same word/construction in new contexts).
# One clean hit retires it and the drill moves on to other things in that skill.

import random as _random  # noqa: E402

_RETEST_GAP = (3, 5)      # how many cards later a miss comes back
_RETEST_BATCH = 3         # targeted cards generated per escalated lapse
_PARKED = 1 << 30         # due_pos sentinel while a re-test is out for grading
_retest_lock = threading.Lock()


def _pos():
    try:
        return int(srs.get_setting("drill_pos", 0))
    except (TypeError, ValueError):
        return 0


def _bump_pos(by=1):
    p = _pos() + by
    srs.set_setting("drill_pos", p)
    return p


def _lapse_miss(lemma, skill, item_id):
    """Record (or deepen) a lapse for a missed card and schedule its re-test."""
    if not skill:
        return
    due = _pos() + _random.randint(*_RETEST_GAP)
    c = _c()
    c.execute("INSERT OR IGNORE INTO drill_lapse(lemma, skill) VALUES(?,?)", (lemma, skill))
    c.execute(
        """UPDATE drill_lapse SET misses = misses + 1, clears = 0, resolved = 0,
             due_pos = ?, src_item_id = COALESCE(src_item_id, ?)
           WHERE lemma IS ? AND skill = ?""", (due, item_id, lemma, skill))
    row = c.execute("SELECT id, misses FROM drill_lapse WHERE lemma IS ? AND skill = ?",
                    (lemma, skill)).fetchone()
    c.commit()
    c.close()
    if row and row["misses"] >= 2:                 # escalate — drill this specific thing
        _gen_retest_async(row["id"])


def _lapse_clear(lemma, skill):
    """A correct answer on this (lemma, skill) retires an open lapse."""
    if not skill:
        return
    c = _c()
    c.execute(
        """UPDATE drill_lapse SET clears = clears + 1, resolved = 1
           WHERE resolved = 0 AND skill = ? AND lemma IS ?""", (skill, lemma))
    c.execute("DELETE FROM drill_items WHERE retest_for IN "
              "(SELECT id FROM drill_lapse WHERE resolved = 1) AND verdict IS NULL")
    c.commit()
    c.close()


def _retest_graded(lapse_id, verdict):
    """Grade landed on a re-test card. Resolve on a hit; re-arm + escalate on a miss."""
    c = _c()
    lap = c.execute("SELECT * FROM drill_lapse WHERE id = ?", (lapse_id,)).fetchone()
    if not lap:
        c.close()
        return
    if verdict == "right":
        c.execute("UPDATE drill_lapse SET clears = clears + 1, resolved = 1 WHERE id = ?",
                  (lapse_id,))
        c.execute("DELETE FROM drill_items WHERE retest_for = ? AND verdict IS NULL", (lapse_id,))
        c.commit()
        c.close()
        return
    due = _pos() + _random.randint(*_RETEST_GAP)
    c.execute("UPDATE drill_lapse SET misses = misses + 1, due_pos = ? WHERE id = ?",
              (due, lapse_id))
    c.commit()
    c.close()
    _gen_retest_async(lapse_id)


def _clone_item(c, src_id, lapse_id):
    """Copy a missed drill_items row as a fresh, served, ungraded re-test card,
    using the caller's open connection."""
    s = c.execute("SELECT * FROM drill_items WHERE id = ?", (src_id,)).fetchone()
    if not s:
        return None
    cur = c.execute(
        """INSERT INTO drill_items(band, lemma, kind, skill, prompt, given, answer,
                                   target, note, contrast, retest_for, served_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
        (s["band"], s["lemma"], s["kind"], s["skill"], s["prompt"], s["given"],
         s["answer"], s["target"], s["note"], s["contrast"], lapse_id))
    return dict(c.execute("SELECT * FROM drill_items WHERE id = ?", (cur.lastrowid,)).fetchone())


def _gen_retest(lapse_id):
    """One LLM call → a few targeted cards drilling a lapse's exact word/skill in
    fresh contexts. Stored with retest_for set."""
    c = _c()
    lap = c.execute("SELECT * FROM drill_lapse WHERE id = ? AND resolved = 0", (lapse_id,)).fetchone()
    if not lap:
        c.close()
        return 0
    have = c.execute("SELECT COUNT(*) n FROM drill_items WHERE retest_for = ? AND verdict IS NULL",
                     (lapse_id,)).fetchone()["n"]
    lap = dict(lap)
    c.close()
    if have >= _RETEST_BATCH or not lap["lemma"]:
        return 0
    gloss = store.gloss_for(lap["lemma"]) or ""
    cc = grammar.concept(lap["skill"])
    concept = ({"id": cc.id, "level": cc.level, "hint": cc.hint or cc.explain,
                "traps": cc.traps, "title": cc.title} if cc else lap["skill"])
    try:
        out = llm.drill_retest_cards(lap["lemma"], gloss, concept, n=_RETEST_BATCH)
    except Exception as e:  # noqa: BLE001
        print(f"[drill] retest gen failed: {e}", flush=True)
        return 0
    band, kind = _band(), _kind_for(cc)
    c = _c()
    n = 0
    for card in (out.get("cards") or []):
        f = _card_row(card)
        if not f["ok"]:
            continue
        c.execute(
            """INSERT INTO drill_items(band, lemma, kind, skill, prompt, given, answer,
                                       target, note, contrast, retest_for)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (band, lap["lemma"], kind, lap["skill"], f["prompt"],
             f["given"], f["answer"], f["target"], f["note"], f["contrast"], lapse_id))
        n += 1
    c.commit()
    c.close()
    return n


def _gen_retest_async(lapse_id):
    def run():
        if not _retest_lock.acquire(blocking=False):
            return
        try:
            _gen_retest(lapse_id)
        finally:
            _retest_lock.release()
    threading.Thread(target=run, daemon=True).start()


def _due_retests(need):
    """Re-test cards whose scheduled position has arrived. Marked served + parked
    so they aren't re-served until graded."""
    if need <= 0:
        return []
    pos = _pos()
    c = _c()
    laps = c.execute(
        "SELECT * FROM drill_lapse WHERE resolved = 0 AND due_pos > 0 AND due_pos <= ? "
        "ORDER BY misses DESC, due_pos ASC LIMIT ?", (pos, need)).fetchall()
    out = []
    for lap in laps:
        item = None
        if lap["misses"] >= 2:
            r = c.execute(
                "SELECT * FROM drill_items WHERE retest_for = ? AND verdict IS NULL "
                "AND served_at IS NULL ORDER BY id LIMIT 1", (lap["id"],)).fetchone()
            if r:
                c.execute("UPDATE drill_items SET served_at = datetime('now') WHERE id = ?",
                          (r["id"],))
                item = dict(r)
            else:
                _gen_retest_async(lap["id"])       # not ready yet — try again next serve
                continue
        else:
            if not lap["src_item_id"]:
                c.execute("UPDATE drill_lapse SET resolved = 1 WHERE id = ?", (lap["id"],))
                continue
            item = _clone_item(c, lap["src_item_id"], lap["id"])
            if not item:
                c.execute("UPDATE drill_lapse SET resolved = 1 WHERE id = ?", (lap["id"],))
                continue
        c.execute("UPDATE drill_lapse SET due_pos = ? WHERE id = ?", (_PARKED, lap["id"]))
        out.append({**_public(item), "retest": True})
    c.commit()
    c.close()
    return out


def _store_cards(cards, band, *, extra_cols="", extra_vals=()):
    """INSERT a batch of LLM cards; `skill`/`kind` come from each card's concept."""
    c = _c()
    n = 0
    for card in cards or []:
        f = _card_row(card)
        if not f["ok"]:
            continue
        c.execute(
            f"""INSERT INTO drill_items(band, lemma, kind, skill, prompt, given, answer,
                                        target, note, contrast{extra_cols})
                VALUES(?,?,?,?,?,?,?,?,?,?{',?' * len(extra_vals)})""",
            (band, f["lemma"], f["kind"], f["skill"], f["prompt"], f["given"], f["answer"],
             f["target"], f["note"], f["contrast"], *extra_vals))
        n += 1
    c.commit()
    c.close()
    return n


def _generate_batch():
    """One LLM call → a batch of drill cards, one per grammar concept, using
    vocabulary from the current frequency band."""
    band = _band()
    lo, hi = BANDS[band]
    concepts = grammar.pick_concepts(_BATCH, band_level=_grammar_level())
    words = [w["lemma"] for w in store.freq_sample(lo, hi, n=_BATCH + 8,
                                                   avoid=_recent_lemmas())]
    if not concepts:
        return 0
    try:
        out = llm.drill_cards(concepts, lo, hi, words)
    except Exception as e:  # noqa: BLE001
        print(f"[drill] generation failed: {e}", flush=True)
        return 0
    return _store_cards(out.get("cards"), band)


def learn_concept(cid, n=6):
    """The learner asked to practise ONE rule — generate a burst of `focus` cards
    for it that jump the queue."""
    cc = grammar.concept(cid)
    if not cc:
        return 0
    lo, hi = BANDS[_band()]
    words = [w["lemma"] for w in store.freq_sample(lo, hi, n=12, avoid=_recent_lemmas())]
    concept = {"id": cc.id, "level": cc.level, "hint": cc.hint or cc.explain,
               "traps": cc.traps, "title": cc.title}
    try:
        out = llm.drill_concept_cards(concept, n=n, words=words)
    except Exception as e:  # noqa: BLE001
        print(f"[drill] learn-concept gen failed: {e}", flush=True)
        return 0
    return _store_cards(out.get("cards"), _band(),
                        extra_cols=", focus", extra_vals=(1,))


def _topup(force=False):
    """Generate ahead if the unseen buffer is low. Serialised; safe in a thread."""
    if not _topup_lock.acquire(blocking=False):
        return
    try:
        c = _c()
        unseen = _servable_count(c)
        c.close()
        rounds = 0
        while (force or unseen < _TARGET_BUFFER) and rounds < 3:
            made = _generate_batch()
            if not made:
                break
            unseen += made
            rounds += 1
            force = False
    finally:
        _topup_lock.release()


def topup_async(force=False):
    threading.Thread(target=_topup, kwargs={"force": force}, daemon=True).start()


def next_items(n=1):
    """Serve the next card(s): due re-tests first, then any 'learn this rule'
    focus burst, then fresh cards from the band buffer. Kicks a background
    top-up when the buffer runs low."""
    n = max(1, n)
    out = _due_retests(n)
    c = _c()
    rows = []
    if len(out) < n:
        rows = c.execute(
            f"""SELECT * FROM drill_items
                WHERE verdict IS NULL AND (
                      (COALESCE(focus,0)=1 AND served_at IS NULL)
                      OR ({_SERVABLE}))
                ORDER BY COALESCE(focus,0) DESC, served_at IS NULL DESC, id
                LIMIT ?""", (n - len(out),)).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            c.execute(f"UPDATE drill_items SET served_at=datetime('now') "
                      f"WHERE id IN ({','.join('?' * len(ids))})", ids)
            c.commit()
    left = _servable_count(c)
    c.close()
    out += [_public(dict(r)) for r in rows]
    if out:
        _bump_pos(len(out))
    if left < _MIN_BUFFER:
        topup_async()
    return out


def _jsonlist(r, col):
    try:
        v = json.loads(r[col]) if r.get(col) else []
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _given(r):
    return _jsonlist(r, "given")


def _public(r):
    cc = grammar.concept(r.get("skill") or "")
    return {"id": r["id"], "kind": r["kind"], "skill": r.get("skill"),
            "concept": r.get("skill"),
            "concept_title": cc.title if cc else None,
            "concept_level": cc.level if cc else None,
            "lemma": r["lemma"],
            "prompt": r["prompt"], "given": _given(r), "answer": r["answer"],
            "target": _jsonlist(r, "target"), "note": r["note"], "contrast": r.get("contrast"),
            "retest": bool(r.get("retest_for")), "focus": bool(r.get("focus")),
            "band": r["band"], "card_id": r["card_id"]}


def _shuffle_pair(item):
    """"читать / прочитать" -> the two aspects in random order, so the front never
    gives away which is perfective by position."""
    parts = [p.strip() for p in item.split("/")]
    if len(parts) == 2 and all(parts):
        _random.shuffle(parts)
        return " / ".join(parts)
    return item


def _card_row(card):
    """Common field extraction for an LLM drill card dict → INSERT tuple parts.
    `skill` = the catalog concept id; `kind` is derived from its category."""
    prompt = (card.get("prompt") or "").strip()
    answer = (card.get("answer") or "").strip().replace("́", "")
    cid = (card.get("concept") or card.get("skill") or "").strip()
    cc = grammar.concept(cid)
    kind = _kind_for(cc)
    given = [_shuffle_pair(str(g).strip().replace("́", ""))
             for g in (card.get("given") or []) if str(g).strip()]
    tgt = [str(t).strip().replace("́", "") for t in (card.get("target") or []) if str(t).strip()]
    ok = bool(prompt and answer)
    # an aspect card must offer BOTH partners on the front, or it gives itself away
    if kind == "aspect" and not any("/" in g for g in given):
        ok = False
    return {
        "ok": ok, "prompt": prompt, "answer": answer,
        "given": json.dumps(given, ensure_ascii=False) if given else None,
        "target": json.dumps(tgt, ensure_ascii=False) if tgt else None,
        "note": (card.get("note") or "").strip() or None,
        "contrast": (card.get("contrast") or "").strip() or None,
        "lemma": (card.get("lemma") or "").strip().split()[0].replace("́", "").lower() or None
        if card.get("lemma") else None,
        "kind": kind, "skill": cc.id if cc else None,
    }


def grade(item_id, verdict):
    # "easy" = "got it, and stop showing me this so much" — stored verbatim, but
    # behaves like a right answer for streaks / lapses / band movement.
    verdict = verdict if verdict in ("right", "wrong", "easy") else "wrong"
    ok = verdict != "wrong"
    c = _c()
    r = c.execute("SELECT verdict, lemma, skill, retest_for FROM drill_items WHERE id=?",
                  (item_id,)).fetchone()
    if not r:
        c.close()
        return None
    fresh = r["verdict"] is None
    lemma, skill, retest_for = r["lemma"], r["skill"], r["retest_for"]
    c.execute("UPDATE drill_items SET verdict=?, graded_at=datetime('now') WHERE id=?",
              (verdict, item_id))
    c.commit()
    c.close()
    if not fresh:
        return {"moved": False, **state()}
    if retest_for:                                 # remediation card — no band effect
        _retest_graded(retest_for, "right" if ok else "wrong")
        return {"moved": False, "retest": True, **state()}
    if not ok:
        _lapse_miss(lemma, skill, item_id)
    else:
        _lapse_clear(lemma, skill)
    moved = _apply_verdict("right" if ok else "wrong")
    eased = None
    if verdict == "easy" and skill:
        try:
            import grammar
            if grammar.concept_stats().get(skill, {}).get("mastery") == "easy":
                cc = grammar.concept(skill)
                eased = cc.title if cc else skill
        except Exception:  # noqa: BLE001
            pass
    if moved:
        # Keep a short bridge of the just-finished difficulty so the learner never
        # stalls, then drop the deep backlog and generate the new band behind it.
        c = _c()
        # cards already handed to the client stay (it will grade them); of the
        # untouched buffer keep only a short bridge, drop the rest.
        keep = [row["id"] for row in c.execute(
            "SELECT id FROM drill_items WHERE verdict IS NULL AND served_at IS NULL "
            "AND retest_for IS NULL ORDER BY id LIMIT ?", (_BRIDGE,)).fetchall()]
        skip = "" if not keep else f" AND id NOT IN ({','.join('?' * len(keep))})"
        c.execute("DELETE FROM drill_items WHERE verdict IS NULL AND served_at IS NULL "
                  "AND retest_for IS NULL" + skip, keep)
        c.commit()
        c.close()
        topup_async(force=True)
    return {"moved": moved, "eased": eased, **state()}


def session_suggest(item_ids, limit=8):
    """End-of-session: from the cards graded this session, the ones the learner
    MISSED and should be able to do (at or below their current band), deduped by
    word+construction, repeat-offenders first. Shape matches a savable card."""
    ids = [int(x) for x in item_ids if str(x).strip().lstrip("-").isdigit()]
    if not ids:
        return []
    ceiling = _band() + 2      # a rough drop mid-session shouldn't hide near-level misses
    c = _c()
    rows = c.execute(
        f"""SELECT * FROM drill_items
            WHERE id IN ({','.join('?' * len(ids))}) AND verdict='wrong'""", ids).fetchall()
    lapses = {(l["lemma"], l["skill"]): l["misses"]
              for l in c.execute("SELECT lemma, skill, misses FROM drill_lapse").fetchall()}
    c.close()
    best = {}
    for r in rows:
        if r["band"] > ceiling:                     # genuinely above their level — skip
            continue
        key = (r["lemma"], r["skill"])
        misses = lapses.get(key, 1)
        cand = {**_public(dict(r)), "misses": misses,
                "already_card": bool(r["card_id"])}
        if key not in best or misses > best[key]["misses"]:
            best[key] = cand
    out = sorted(best.values(), key=lambda x: (-x["misses"], x["band"]))
    return out[:max(1, limit)]


def save_as_card(item_id):
    c = _c()
    r = c.execute("SELECT * FROM drill_items WHERE id=?", (item_id,)).fetchone()
    if not r:
        c.close()
        return None
    r = dict(r)
    c.close()
    if r["card_id"]:
        return srs.get_card(r["card_id"])
    meta = {"kind": r["kind"], "skill": r["skill"], "given": _given(r),
            "target": _jsonlist(r, "target"), "contrast": r["contrast"]}
    card = srs.create_production_card(r["prompt"], r["answer"], note=r["note"],
                                     speak_ref=f"drill:{item_id}", meta=meta)
    c = _c()
    c.execute("UPDATE drill_items SET card_id=? WHERE id=?", (card["id"], item_id))
    c.commit()
    c.close()
    return card
