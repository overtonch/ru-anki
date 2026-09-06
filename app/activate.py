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
LEVELS = ("a1", "a2", "b1", "b1+", "b2", "b2+")
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
}
_MATURE_REPS = 4          # an item counts as "active" once it's stuck this well
_FN_TAGS = ("CONJ", "PREP", "PRCL", "NPRO", "Apro")   # skip these as word targets


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
        "mix": float(_get("mix", 0.65)),        # share of new items that are verbs
        "per_day": int(_get("per_day", 10)),    # new items introduced per day
    }


def set_settings(level=None, mix=None, per_day=None):
    if level in LEVELS:
        srs.set_setting("activate_level", level)
    if mix is not None:
        srs.set_setting("activate_mix", str(max(0.0, min(1.0, float(mix)))))
    if per_day is not None:
        srs.set_setting("activate_per_day", str(max(0, min(60, int(per_day)))))
    return settings()


# ---------------------------------------------------------------- curriculum

_BARE_OBLIQUE = ("gen", "dat", "instr", "ablt", "loct")


def _hardness(patterns, trap):
    """0 = plain accusative / obvious · 3 = the government an English speaker
    reliably gets wrong. Drives the order verbs are introduced in."""
    h = 0
    if (trap or "").strip():
        h += 2
    preps, bare = 0, 0
    for p in patterns or []:
        g = (p.get("gov") or "").lower()
        if "+" in g and "acc" not in g.split("+")[1]:
            preps += 1
        elif "no prep" in g and any(b in g for b in _BARE_OBLIQUE):
            bare += 1
    if preps >= 2:
        h += 2
    elif preps == 1 or bare:
        h += 1
    return max(0, min(3, h))


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
                          (_hardness(json.loads(r["government"] or "[]"), r["trap"]), r["verb"]))
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
             _hardness(pats, r.get("trap"))))
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
    """Add up to `n` new items, verb/word split by the mix setting, respecting
    today's per-day budget."""
    if n <= 0:
        return []
    today = srs._day_start_iso()[:10]
    added_today = c.execute(
        "SELECT COUNT(*) n FROM activate_items WHERE substr(introduced_at,1,10) >= ?",
        (today,)).fetchone()["n"]
    budget = max(0, settings()["per_day"] - added_today)
    n = min(n, budget)
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


def grade(item_id, rating, produced=None, task="", government=""):
    """Record a grade, optionally check a typed attempt, reschedule."""
    c = _c()
    row = c.execute("SELECT * FROM activate_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        c.close()
        return {"error": "no such item"}
    it = dict(row)
    check = None
    if produced and produced.strip():
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
    _reschedule(c, it, int(rating))
    c.execute("INSERT INTO activate_log(item_id, rating, produced, category) VALUES(?,?,?,?)",
              (item_id, int(rating), (produced or "")[:400],
               (check or {}).get("category")))
    c.commit()
    nd = c.execute("SELECT due FROM activate_items WHERE id=?", (item_id,)).fetchone()["due"]
    c.close()
    return {"check": check, "next_due": srs._human_delta(nd)}


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
    weak = [{"category": r["category"], "n": r["n"]} for r in q(
        """SELECT category, COUNT(*) n FROM activate_log
           WHERE category IS NOT NULL AND category NOT IN ('none','')
             AND at >= datetime('now','-30 days')
           GROUP BY category ORDER BY n DESC LIMIT 5""")]
    verb_pool = q("SELECT COUNT(*) n FROM activate_verbs").fetchone()["n"]
    c.close()
    return {"total": total, "by_kind": by_kind, "active": active, "due": due,
            "done_today": done_today, "weak_spots": weak, "verb_pool": verb_pool,
            **settings()}
