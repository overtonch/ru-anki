"""Speaking activation — turning passive vocabulary into active production.

A silent, phone-friendly drill. The app names a TARGET and a concrete thought to
express; the learner forms the Russian in their head (or types it) and checks.
Items are SRS-scheduled (a light SM-2). Three tracks, distinguished by `kind`:

  * verb  — a high-frequency verb + its GOVERNMENT (which case / preposition).
            The learner's stated #1 weakness. Curriculum: `activate_verbs`
            (bundled `app/data/activate/verbs.json.gz`).
  * word  — a common content word to retrieve and use. Introduced from the
            frequency list, newest-frequent first.
  * frame — (not built yet) a grammatical construction to wield in context.

One mode, `mix` controls the verb/word ratio. See LEARNING.md for the rationale.
"""
import gzip
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db          # noqa: E402
import llm         # noqa: E402
import srs         # noqa: E402
import store       # noqa: E402

_DATA = os.path.join(_HERE, "data", "activate")

# the difficulty scale — a CEFR ladder. The learner sets where the English
# prompts pitch; "acing b2" means producing b2-level thoughts on demand.
LEVELS = ("a1", "a2", "b1", "b1+", "b2", "b2+", "c1", "c2")
_LEVEL_GUIDE = {
    "a1": "4–6 words, ONE clause, present tense. e.g. 'I drink coffee every morning.'",
    "a2": "one everyday sentence; past or future fine; at most a simple 'that' clause. "
          "e.g. 'Yesterday I told my friend I was tired.'",
    "b1": "a compound sentence — two ideas joined by and / but / because / so, or "
          "simple reported speech. e.g. 'I wanted to come but I had too much work.'",
    "b1+": "a sentence with a subordinate clause AND a real aspect or modal choice. "
           "e.g. 'If I get time tomorrow I'll finish what I started last week.'",
    "b2": "a genuinely complex thought: two or three clauses, a conditional or "
          "hypothetical, a чтобы-clause, or a hedged opinion — the kind of sentence "
          "you stumble on when speaking.",
    "b2+": "near-native complexity — nested clauses, concession (although / even "
           "though), idiomatic phrasing, a subtle modal or evidential shade.",
    "c1": "a nuanced, structurally rich sentence — subordination plus a discourse "
          "move (concession, contrast, qualification, reported stance); the kind "
          "of thing that separates a fluent speaker from a competent one.",
    "c2": "sophisticated and idiomatic, effortless-sounding — register control, "
          "set phrases, a precise modal or evidential shade, near-native word "
          "order and information structure.",
}
_MATURE_REPS = 4          # an item counts as "active" once it's stuck this well
_FN_TAGS = ("CONJ", "PREP", "PRCL", "NPRO", "Apro")   # skip these as word targets

# adaptive difficulty — the share of retrievals the drill aims to keep SUCCESSFUL.
# The "85% rule" (Wilson, Shenhav, Straccia & Cohen, Nature Communications 2019)
# puts the optimal training-error rate for an adjustable-difficulty task at ≈15%,
# i.e. ~85% correct; retrieval-practice work likewise favours mostly-successful
# recall (failed retrievals teach little). We steer toward this band.
_TARGET_SUCCESS = 0.85
_ADAPT_UP, _ADAPT_DOWN = 0.90, 0.72   # move a rung when the recent rate leaves the band
_ADAPT_WINDOW = 8                     # graded attempts since the last level change


def _c():
    return store.connect()


# ---------------------------------------------------------------- settings

def _get(key, default):
    v = srs.get_setting("activate_" + key, default)
    return v if v is not None else default


def settings():
    lvl = _get("level", "a2")
    if lvl not in LEVELS:
        lvl = {"gentle": "a1", "standard": "b1", "stretch": "b2"}.get(lvl, "a2")
    return {
        "level": lvl,
        "levels": list(LEVELS),
        "mix": float(_get("mix", 0.65)),         # share of new items that are verbs
        "per_day": int(_get("per_day", 0)),      # new items/day; 0 = unlimited (endless)
        "auto": str(_get("auto", "1")) not in ("0", "false", ""),
        "target_success": _TARGET_SUCCESS,
    }


def set_settings(level=None, mix=None, per_day=None, auto=None):
    if level in LEVELS:
        srs.set_setting("activate_level", level)
        srs.set_setting("activate_level_changed_at", srs._iso(srs._utc()))
    if mix is not None:
        srs.set_setting("activate_mix", str(max(0.0, min(1.0, float(mix)))))
    if per_day is not None:
        srs.set_setting("activate_per_day", str(max(0, min(200, int(per_day)))))
    if auto is not None:
        srs.set_setting("activate_auto", "1" if auto else "0")
    return settings()


# ---------------------------------------------------------------- curriculum

# copulas — the instrumental complement is expected grammar, not a trap
_COPULA = {"быть", "стать", "становиться", "являться", "оставаться", "казаться",
           "оказаться", "считаться", "называться", "представляться", "выглядеть"}
# the commonest verbs, whose government the learner already wields fluently —
# never let the scoring float these to the top of the queue
_TRIVIAL = {"быть", "сказать", "говорить", "знать", "думать", "хотеть", "мочь",
            "стать", "дать", "давать", "делать", "сделать", "идти", "ходить",
            "видеть", "слышать", "любить", "жить", "работать", "понимать",
            "понять", "спросить", "рассказать", "получить", "брать", "взять"}
# prepositions whose case-pairing an English speaker reliably gets wrong
_HARD_PREP = {"к", "от", "у", "из", "за", "над", "под", "перед", "до", "без",
              "для", "через", "по"}
_SOFT_PREP = {"о", "об", "про"}          # transparent "about" — easy


def _first_prep(g):
    """The bare preposition that opens a government string like 'на + acc'."""
    head = g.split("+")[0].strip().strip("()/ ")
    if not head:
        return ""
    return head.split("/")[0].split()[0].strip()


def _hardness(verb, patterns, trap):
    """0 = plain direct object / obvious … 4 = a bare genitive/instrumental object
    (the government English speakers get wrong most). Scored off the verb's
    PRIMARY pattern; drives the order verbs are introduced in so the traps —
    заниматься чем, бояться чего, зависеть от … — come first, not смотреть/идти."""
    if verb in _COPULA or verb in _TRIVIAL:
        return 1 if verb in _COPULA else 0
    pats = patterns or []
    if not pats:
        return 0
    govs = [(p.get("gov") or "").strip().lower() for p in pats]
    g0 = govs[0]
    has_acc = any(x.startswith("acc") for x in govs)

    if g0.startswith("acc") or g0.startswith("+ inf") or g0 == "infinitive" \
            or "куда" in g0 or "где" in g0:
        score = 0
    elif "no prep" in g0 or "no preposition" in g0 or g0 in ("gen", "instr", "dat"):
        if "gen" in g0 or "instr" in g0 or "ablt" in g0:
            score = 4               # bare genitive / instrumental object — the worst
        elif "dat" in g0:
            score = 1 if has_acc else 3   # recipient dative vs. dative-as-object
        else:
            score = 2
    elif "+" in g0:
        prep = _first_prep(g0)
        if prep in _SOFT_PREP:
            score = 1
        elif prep in _HARD_PREP:
            score = 3
        elif prep in ("в", "на"):
            score = 2                # directional в/на — guessable enough
        elif prep in ("с", "со"):
            score = 2
        else:
            score = 2
    else:
        score = 1

    if len([g for g in govs if g and g != "infinitive"]) >= 4:
        score += 1                  # sprawling, sense-dependent government
    return max(0, min(4, score))


def ensure_verbs():
    """Load the bundled verb-government list into activate_verbs once, scoring
    each verb's government difficulty."""
    c = _c()
    have = c.execute("SELECT COUNT(*) n FROM activate_verbs").fetchone()["n"]
    if have:
        # backfill hardness if the column was just added (all still the default)
        spread = c.execute("SELECT MIN(hardness) lo, MAX(hardness) hi FROM activate_verbs").fetchone()
        if spread["lo"] == spread["hi"]:
            for r in c.execute("SELECT verb, government, trap FROM activate_verbs"):
                c.execute("UPDATE activate_verbs SET hardness=? WHERE verb=?",
                          (_hardness(r["verb"], json.loads(r["government"] or "[]"), r["trap"]), r["verb"]))
            c.commit()
        c.close()
        return have
    path = os.path.join(_DATA, "verbs.json.gz")
    if not os.path.exists(path):
        c.close()
        return 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rows = json.load(f)
    for r in rows:
        pats = r.get("patterns") or []
        c.execute(
            "INSERT OR IGNORE INTO activate_verbs(verb, rank, gloss, aspect_pair, government, trap, hardness) "
            "VALUES(?,?,?,?,?,?,?)",
            (r["verb"], r.get("rank"), r.get("gloss"), r.get("aspect_pair"),
             json.dumps(pats, ensure_ascii=False), r.get("trap"),
             _hardness(r["verb"], pats, r.get("trap"))))
    c.commit()
    n = c.execute("SELECT COUNT(*) n FROM activate_verbs").fetchone()["n"]
    c.close()
    return n


def _gov_str(patterns):
    if not patterns:
        return ""
    return "; ".join(f"{p.get('gov', '')} — {p.get('role', '')}" for p in patterns)


# ---------------------------------------------------------------- introduce

def _introduce(c, n):
    """Add up to `n` new items, verb/word split by the mix setting. `per_day` 0
    means unlimited — the drill is endless, not gated by a daily new-card cap."""
    if n <= 0:
        return []
    per_day = settings()["per_day"]
    if per_day > 0:
        today = srs._day_start_iso()[:10]
        added_today = c.execute(
            "SELECT COUNT(*) n FROM activate_items WHERE substr(introduced_at,1,10) >= ?",
            (today,)).fetchone()["n"]
        n = min(n, max(0, per_day - added_today))
        if n <= 0:
            return []
    mix = settings()["mix"]
    want_verbs = round(n * mix)
    out = []

    ensure_verbs()
    # the government an English speaker gets wrong comes first; within a
    # difficulty tier, commonest first
    vrows = c.execute(
        """SELECT v.verb, v.gloss, v.government FROM activate_verbs v
           WHERE v.verb NOT IN (SELECT target FROM activate_items WHERE kind='verb')
           ORDER BY v.hardness DESC, v.rank ASC LIMIT ?""", (want_verbs,)).fetchall()
    for r in vrows:
        cur = c.execute(
            "INSERT INTO activate_items(kind, target, gloss) VALUES('verb',?,?)",
            (r["verb"], r["gloss"]))
        out.append(cur.lastrowid)

    want_words = n - len(out)
    if want_words > 0:
        wrows = c.execute(
            """SELECT f.normalized_text w FROM freq f
               WHERE f.rank <= 3500
                 AND f.normalized_text NOT IN (SELECT normalized_text FROM stoplist)
                 AND f.normalized_text NOT IN (SELECT target FROM activate_items WHERE kind='word')
                 AND f.normalized_text NOT IN (SELECT verb FROM activate_verbs)
               ORDER BY f.rank LIMIT ?""", (want_words * 3,)).fetchall()
        picked = 0
        for r in wrows:
            w = r["w"]
            tag = str(db._morph().parse(w)[0].tag)
            if any(t in tag for t in _FN_TAGS) or len(w) < 3:
                continue
            gl = store.gloss_for(w)
            cur = c.execute(
                "INSERT INTO activate_items(kind, target, gloss) VALUES('word',?,?)",
                (w, gl))
            out.append(cur.lastrowid)
            picked += 1
            if picked >= want_words:
                break
    c.commit()
    return out


# ---------------------------------------------------------------- schedule

def _reschedule(c, item, rating):
    ease = item["ease"]
    itv = item["interval_d"]
    streak = item["streak"]
    lapses = item["lapses"]
    if rating >= 5:                       # "I've got this" — retire the item
        c.execute(
            "UPDATE activate_items SET reps=MAX(reps,5), streak=MAX(streak,3), "
            "interval_d=180, due=?, last_seen=datetime('now') WHERE id=?",
            (srs._iso(srs._utc() + srs._dt.timedelta(days=180)), item["id"]))
        return
    if rating <= 1:
        ease = max(1.5, ease - 0.2)
        itv = 0.0
        streak = 0
        lapses += 1
    elif rating == 2:
        ease = max(1.5, ease - 0.05)
        itv = max(1.0, (itv or 1.0) * 1.2)
        streak += 1
    elif rating == 3:
        itv = 1.0 if itv < 1 else itv * ease
        streak += 1
    else:
        ease = min(3.0, ease + 0.05)
        itv = 3.0 if itv < 1 else itv * ease * 1.3
        streak += 1
    due = srs._iso(srs._utc() + srs._dt.timedelta(days=itv) if itv >= 1
                   else srs._utc() + srs._dt.timedelta(minutes=10))
    c.execute(
        "UPDATE activate_items SET reps=reps+1, lapses=?, streak=?, ease=?, "
        "interval_d=?, due=?, last_seen=datetime('now') WHERE id=?",
        (lapses, streak, round(ease, 3), round(itv, 2), due, item["id"]))


# ---------------------------------------------------------------- serve one

def next_item():
    """The next thing to practise, with its prompt already generated."""
    st = settings()
    c = _c()
    now = srs._iso(srs._utc())
    row = c.execute(
        "SELECT * FROM activate_items WHERE due <= ? ORDER BY due ASC LIMIT 1",
        (now,)).fetchone()
    if not row:
        made = _introduce(c, 1)
        if made:
            row = c.execute("SELECT * FROM activate_items WHERE id=?", (made[0],)).fetchone()
    if not row:
        # curriculum exhausted for now — keep it endless by serving the item
        # that's closest to due rather than dead-ending
        row = c.execute(
            "SELECT * FROM activate_items ORDER BY due ASC LIMIT 1").fetchone()
    if not row:
        c.close()
        return None
    it = dict(row)
    gov = ""
    if it["kind"] == "verb":
        v = c.execute("SELECT government, trap, aspect_pair FROM activate_verbs WHERE verb=?",
                      (it["target"],)).fetchone()
        if v:
            gov = _gov_str(json.loads(v["government"] or "[]"))
            it["trap"] = v["trap"]
            it["aspect_pair"] = v["aspect_pair"]
            it["patterns"] = json.loads(v["government"] or "[]")
    angles = json.loads(it.get("angles") or "[]")
    c.close()

    try:
        p = llm.activate_prompt(it["target"], it["gloss"] or "", kind=it["kind"],
                                government=gov, level=st["level"],
                                level_guide=_LEVEL_GUIDE.get(st["level"], _LEVEL_GUIDE["b1"]),
                                avoid=angles[-4:])
    except Exception as e:  # noqa: BLE001
        print(f"[activate] prompt {it['target']}: {e}", flush=True)
        p = {"task": f"Say something true about your life using «{it['target']}».",
             "model": "", "note": None}

    return {
        "id": it["id"], "kind": it["kind"], "target": it["target"],
        "gloss": it["gloss"], "government": gov, "patterns": it.get("patterns") or [],
        "trap": it.get("trap"), "aspect_pair": it.get("aspect_pair"),
        "task": p.get("task"), "model": p.get("model"), "note": p.get("note"),
        "reps": it["reps"], "streak": it["streak"], "level": st["level"],
    }


_MISS_TAGS = ("government", "case", "aspect", "verb-form", "agreement",
              "word-order", "lexical", "no-start")


def check_attempt(item_id, produced, task="", government=""):
    """Run the LLM check on a typed attempt WITHOUT grading or rescheduling —
    the learner still picks their own rating afterwards."""
    if not produced or not produced.strip():
        return None
    c = _c()
    row = c.execute("SELECT target FROM activate_items WHERE id=?", (item_id,)).fetchone()
    c.close()
    if not row:
        return None
    try:
        return llm.activate_check(row["target"], task, government, produced.strip())
    except Exception as e:  # noqa: BLE001
        print(f"[activate] check {row['target']}: {e}", flush=True)
        return None


def grade(item_id, rating, produced=None, task="", government="", categories=None):
    """Record a grade, optionally check a typed attempt, reschedule.
    `categories` = what the learner flagged as wrong (see _MISS_TAGS)."""
    c = _c()
    row = c.execute("SELECT * FROM activate_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        c.close()
        return {"error": "no such item"}
    it = dict(row)
    check = None
    # only run the LLM check inline for legacy callers (categories is None); the
    # current UI checks via /activate/items/{id}/check first and passes the tags
    if categories is None and produced and produced.strip():
        try:
            check = llm.activate_check(it["target"], task, government, produced.strip())
        except Exception as e:  # noqa: BLE001
            print(f"[activate] check {it['target']}: {e}", flush=True)
    # remember the angle so the next prompt for this item differs
    if task:
        angles = json.loads(it.get("angles") or "[]")
        angles.append(task[:80])
        c.execute("UPDATE activate_items SET angles=? WHERE id=?",
                  (json.dumps(angles[-8:], ensure_ascii=False), item_id))
    tags = [t for t in (categories or []) if t and t in _MISS_TAGS]
    if not tags and (check or {}).get("category") not in (None, "none", ""):
        tags = [check["category"]]
    st = settings()
    _reschedule(c, it, int(rating))
    c.execute("INSERT INTO activate_log(item_id, rating, produced, category, categories, level) "
              "VALUES(?,?,?,?,?,?)",
              (item_id, int(rating), (produced or "")[:400],
               (tags[0] if tags else None),
               (json.dumps(tags, ensure_ascii=False) if tags else None), st["level"]))
    c.commit()
    moved = _adapt_level(c) if st["auto"] else None
    nd = c.execute("SELECT due FROM activate_items WHERE id=?", (item_id,)).fetchone()["due"]
    c.close()
    return {"check": check, "next_due": srs._human_delta(nd), "level_moved": moved}


# ---------------------------------------------------------------- adapt

def _recent_success(c, level=None):
    """(rate, n) over the graded attempts since the last level change. A grade of
    3+ ('got it' / 'easy' / 'I know this') counts as a successful retrieval."""
    since = srs.get_setting("activate_level_changed_at", "") or "1970-01-01"
    # `at` is stored as SQLite datetime() text, `since` as ISO-8601 — normalise both
    rows = c.execute(
        "SELECT rating FROM activate_log WHERE datetime(at) >= datetime(?) "
        + ("AND level = ? " if level else "")
        + "ORDER BY at DESC LIMIT ?",
        ((since, level, _ADAPT_WINDOW) if level else (since, _ADAPT_WINDOW))).fetchall()
    if not rows:
        return None, 0
    ok = sum(1 for r in rows if (r["rating"] or 0) >= 3)
    return ok / len(rows), len(rows)


def _adapt_level(c):
    """Nudge the working CEFR level so the recent success rate stays in the
    target band. Climbs two rungs at once when everything is coming out perfect,
    so a learner with a big passive vocabulary reaches their real ceiling fast.
    Returns the new level if it moved, else None."""
    st = settings()
    rate, n = _recent_success(c, st["level"])
    if rate is None or n < _ADAPT_WINDOW:
        return None
    i = LEVELS.index(st["level"])
    ni = i
    if rate >= _ADAPT_UP and i < len(LEVELS) - 1:
        step = 2 if (rate >= 0.99 and i < len(LEVELS) - 2) else 1
        ni = i + step
    elif rate <= _ADAPT_DOWN and i > 0:
        ni = i - 1
    if ni == i:
        return None
    srs.set_setting("activate_level", LEVELS[ni])
    srs.set_setting("activate_level_changed_at", srs._iso(srs._utc()))
    return LEVELS[ni]


# ---------------------------------------------------------------- calibration

def _fn_word(w):
    tag = str(db._morph().parse(w)[0].tag)
    return any(t in tag for t in _FN_TAGS) or len(w) < 3


def calibration_batch(kind="verb", offset=0, n=30):
    """A batch of not-yet-mastered targets for the learner to sweep — 'which of
    these do you already handle?'. Verbs hardest-government first; words
    commonest first."""
    c = _c()
    done_sub = "SELECT target FROM activate_items WHERE reps >= 5"
    if kind == "verb":
        ensure_verbs()
        rows = c.execute(
            f"""SELECT v.verb t, v.gloss g, v.government gov FROM activate_verbs v
                WHERE v.verb NOT IN ({done_sub})
                ORDER BY v.hardness DESC, v.rank ASC LIMIT ? OFFSET ?""",
            (n, offset)).fetchall()
        total = c.execute(
            f"SELECT COUNT(*) n FROM activate_verbs v WHERE v.verb NOT IN ({done_sub})"
        ).fetchone()["n"]
        items = [{"target": r["t"], "gloss": r["g"],
                  "government": _gov_str(json.loads(r["gov"] or "[]"))} for r in rows]
    else:
        rows = c.execute(
            f"""SELECT f.normalized_text t FROM freq f
                WHERE f.rank <= 4000
                  AND f.normalized_text NOT IN (SELECT normalized_text FROM stoplist)
                  AND f.normalized_text NOT IN (SELECT verb FROM activate_verbs)
                  AND f.normalized_text NOT IN ({done_sub})
                ORDER BY f.rank LIMIT ? OFFSET ?""",
            (n * 2, offset)).fetchall()
        items = []
        for r in rows:
            if _fn_word(r["t"]):
                continue
            items.append({"target": r["t"], "gloss": store.gloss_for(r["t"]), "government": ""})
            if len(items) >= n:
                break
        total = offset + len(items) + (n if len(rows) >= n * 2 else 0)
    c.close()
    return {"items": items, "remaining": max(0, total - offset - len(items)), "kind": kind}


def calibrate(kind, known):
    """Retire every target the learner says they already handle (counts as
    active — a productive-vocabulary claim they're making on purpose)."""
    c = _c()
    due = srs._iso(srs._utc() + srs._dt.timedelta(days=180))
    retired = 0
    for t in (known or []):
        t = (t or "").strip()
        if not t:
            continue
        gloss = None
        if kind == "verb":
            g = c.execute("SELECT gloss FROM activate_verbs WHERE verb=?", (t,)).fetchone()
            gloss = g["gloss"] if g else None
        else:
            try:
                gloss = store.gloss_for(t)
            except Exception:  # noqa: BLE001
                gloss = None
        row = c.execute("SELECT id FROM activate_items WHERE target=? AND kind=?",
                        (t, kind)).fetchone()
        if row:
            c.execute("UPDATE activate_items SET reps=MAX(reps,5), streak=MAX(streak,3), "
                      "interval_d=180, due=?, last_seen=datetime('now') WHERE id=?",
                      (due, row["id"]))
        else:
            c.execute("INSERT INTO activate_items(kind, target, gloss, reps, streak, "
                      "interval_d, due, last_seen) VALUES(?,?,?,5,3,180,?,datetime('now'))",
                      (kind, t, gloss, due))
        retired += 1
    c.commit()
    c.close()
    return {"retired": retired}


# ---------------------------------------------------------------- stats

def active_count():
    c = _c()
    n = c.execute("SELECT COUNT(*) n FROM activate_items WHERE reps >= ? AND streak >= 2",
                  (_MATURE_REPS,)).fetchone()["n"]
    c.close()
    return n


def stats():
    c = _c()
    q = c.execute
    now = srs._iso(srs._utc())
    total = q("SELECT COUNT(*) n FROM activate_items").fetchone()["n"]
    by_kind = {r["kind"]: r["n"] for r in q(
        "SELECT kind, COUNT(*) n FROM activate_items GROUP BY kind")}
    active = q("SELECT COUNT(*) n FROM activate_items WHERE reps >= ? AND streak >= 2",
               (_MATURE_REPS,)).fetchone()["n"]
    due = q("SELECT COUNT(*) n FROM activate_items WHERE due <= ?", (now,)).fetchone()["n"]
    done_today = q("SELECT COUNT(*) n FROM activate_log WHERE at >= ?",
                   (srs._day_start_iso(),)).fetchone()["n"]
    from collections import Counter
    wc = Counter()
    for r in q("""SELECT category, categories FROM activate_log
                  WHERE at >= datetime('now','-30 days')"""):
        got = []
        if r["categories"]:
            try:
                got = [t for t in json.loads(r["categories"]) if t]
            except Exception:  # noqa: BLE001
                got = []
        if not got and r["category"] and r["category"] not in ("none", ""):
            got = [r["category"]]
        wc.update(got)
    weak = [{"category": k, "n": n} for k, n in wc.most_common(6)]
    verb_pool = q("SELECT COUNT(*) n FROM activate_verbs").fetchone()["n"]
    r30 = q("""SELECT COUNT(*) n, SUM(CASE WHEN rating >= 3 THEN 1 ELSE 0 END) ok
               FROM activate_log WHERE at >= datetime('now','-30 days')""").fetchone()
    success_30d = round(r30["ok"] / r30["n"], 3) if r30["n"] else None
    rate_now, rate_n = _recent_success(c)
    c.close()
    return {"total": total, "by_kind": by_kind, "active": active, "due": due,
            "done_today": done_today, "weak_spots": weak, "verb_pool": verb_pool,
            "success_30d": success_30d, "success_recent": rate_now,
            "success_recent_n": rate_n,
            **settings()}
