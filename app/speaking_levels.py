"""The speaking side of the proficiency picture.

Reading proficiency is tracked as a frequency-rank cutoff (`proficiency.py`).
Speaking is different: what matters is not what you *recognise* but what you can
*pull out of your head and say*. This module defines, per CEFR level, the
vocabulary a speaker at that level is expected to be able to produce, and scores
the learner against it using the Activate drill's record of what has actually
stuck (`activate_items` that have matured).

The headline number is `speaking_ord` — a 0..6 position on the CEFR ladder,
directly comparable to the reading level. The stats page plots the two together;
the goal is the gap between them shrinking over time.

CEFR ↔ frequency bands: the ranges below follow the standard frequency-band
mapping used in CEFR-graded wordlist work and the Routledge *Frequency
Dictionary of Russian* (the first ~1k lemmas cover A-level needs, ~2-3k gets you
through B1, ~5k through B2, the C-levels reach well past 10k). The "+" rungs in
the Activate difficulty slider (b1+, b2+) share their base band's vocabulary and
only raise how complex the prompts are.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db          # noqa: E402
import llm         # noqa: E402
import store       # noqa: E402

# (level, rank_lo inclusive, rank_hi exclusive)
BANDS = [
    ("a1", 1, 550),
    ("a2", 550, 1200),
    ("b1", 1200, 2600),
    ("b2", 2600, 5200),
    ("c1", 5200, 10000),
    ("c2", 10000, 20000),
]
_BAND_ORD = {b[0]: i + 1 for i, b in enumerate(BANDS)}   # a1→1 … c2→6

# you "own" a level's productive vocabulary once you can say this share of it.
# Deliberately below 1.0 — nobody actively wields every word in a band, and the
# tail of each band is rare enough that partial command is real command.
_OWN = 0.60

_FN_TAGS = ("CONJ", "PREP", "PRCL", "NPRO", "Apro", "NUMR")
# grammatical adverbs / discourse particles that top the frequency list but
# aren't "vocabulary to activate" — any speaker past the first lessons has them
_SKIP = {"уже", "ещё", "еще", "очень", "только", "нет", "да", "тоже", "также",
         "именно", "даже", "вот", "ведь", "разве", "неужели", "лишь", "почти",
         "совсем", "вообще", "конечно", "наверное", "например", "кстати",
         "впрочем", "однако", "зато", "итак", "значит", "мол", "дескать",
         "вроде", "будто", "словно", "точно", "прямо", "просто", "особенно",
         "довольно", "слишком", "весьма", "крайне", "снова", "опять", "затем",
         "потом", "сейчас", "теперь", "тогда", "здесь", "тут", "там", "туда",
         "сюда", "оттуда", "отсюда", "везде", "всюду", "нигде", "куда", "где",
         "когда", "как", "почему", "зачем", "сколько", "который", "чей"}
_active_cache = {}       # rank_hi → ordered [lemma] list of content words


# ---------------------------------------------------------------- target lists

def _band_words(c, lo, hi):
    """The content lemmas in a frequency band that a learner should be able to
    produce: real dictionary words (drops lemmatiser noise), not function words,
    commonest first. Cached per process — the frequency list is static."""
    n_freq = c.execute("SELECT COUNT(*) n FROM freq").fetchone()["n"]
    key = (lo, hi, n_freq)
    if key in _active_cache:
        return _active_cache[key]
    _active_cache.clear()               # freq changed underneath us — rebuild
    rows = c.execute(
        "SELECT normalized_text w, rank FROM freq WHERE rank >= ? AND rank < ? ORDER BY rank",
        (lo, hi)).fetchall()
    skip = _SKIP
    try:
        import activate
        skip = _SKIP | activate._TRIVIAL | activate._COPULA
    except Exception:  # noqa: BLE001
        pass
    cand = [r["w"] for r in rows if r["w"] and len(r["w"]) >= 3 and r["w"] not in skip]
    ph = ",".join("?" * len(cand)) if cand else "''"
    indict = {r["headword"] for r in c.execute(
        f"SELECT headword FROM dict_ru WHERE headword IN ({ph})", cand)} if cand else set()
    out = []
    for w in cand:
        if w not in indict:
            continue
        tag = str(db._morph().parse(w)[0].tag)
        if any(t in tag for t in _FN_TAGS):
            continue
        out.append(w)
    _active_cache[key] = out
    return out


def _produced(c):
    """Every lemma the learner can actively produce: Activate items that have
    matured (stuck across several spaced retrievals)."""
    return {r["target"] for r in c.execute(
        "SELECT target FROM activate_items WHERE reps >= 4 AND streak >= 2")}


def _emerging(c):
    """Lemmas currently being activated but not yet stuck."""
    return {r["target"] for r in c.execute(
        "SELECT target FROM activate_items WHERE NOT (reps >= 4 AND streak >= 2)")}


# ---------------------------------------------------------------- scoring

def level_progress():
    """Per CEFR band: how much of its productive vocabulary the learner can
    already say, plus the commonest words still to activate."""
    c = store.connect()
    try:
        prod, emer = _produced(c), _emerging(c)
        out = []
        for lvl, lo, hi in BANDS:
            words = _band_words(c, lo, hi)
            wset = set(words)
            active = len(wset & prod)
            emerging = len(wset & emer)
            target = len(words) or 1
            todo = [w for w in words if w not in prod and w not in emer]
            out.append({
                "level": lvl,
                "band": [lo, hi],
                "target": len(words),
                "active": active,
                "emerging": emerging,
                "pct": round(active / target, 3),
                "next": todo[:12],
            })
        return out
    finally:
        c.close()


def _ord_label(o):
    """A 0..6 ladder position → a CEFR-ish label (a1, a2, b1, b1+, …)."""
    if o <= 0.05:
        return "pre-a1"
    base = min(len(BANDS), max(1, int(o) + (0 if o == int(o) else 1)))
    lvl = BANDS[base - 1][0]
    frac = o - (base - 1)
    if frac >= 0.66:
        return lvl
    if frac >= 0.34:
        # partway into the band — mark it with a '+'
        prev = BANDS[base - 2][0] if base >= 2 else "pre-a1"
        return prev + "+"
    return (BANDS[base - 2][0] if base >= 2 else "pre-a1")


def speaking_ord(progress=None):
    """The learner's position on the CEFR ladder for *speaking*, 0..6.
    Full credit for every band whose productive vocab is `_OWN`-covered, then
    partial credit into the first band that isn't."""
    prog = progress or level_progress()
    o = 0.0
    for i, p in enumerate(prog):
        if p["pct"] >= _OWN:
            o = i + 1
        else:
            o = i + min(1.0, p["pct"] / _OWN)
            break
    return round(o, 3)


def reading_ord(cefr):
    """The reading level on the same 0..6 ladder, from its CEFR label."""
    return cefr_ord(cefr)


_CEFR_ORD = {"pre-a1": 0.3, "a1": 1, "a1+": 1.5, "a2": 2, "a2+": 2.5,
             "b1": 3, "b1+": 3.5, "b2": 4, "b2+": 4.5, "c1": 5, "c1+": 5.5,
             "c2": 6}


def cefr_ord(label):
    """Map any CEFR label this app produces — 'b1', 'b1+/b2', 'c1+', 'a2' — to a
    number on the shared 0..6 ladder."""
    t = (label or "").strip().lower().replace(" ", "")
    if not t:
        return 3.0
    if t in _CEFR_ORD:
        return float(_CEFR_ORD[t])
    if "/" in t:                       # 'b1+/b2' → midpoint of the two labels
        parts = [cefr_ord(x) for x in t.split("/") if x]
        return round(sum(parts) / len(parts), 2) if parts else 3.0
    return float(_CEFR_ORD.get(t[:2], 3.0))


def estimate(reading_cefr=None):
    """The speaking-side summary for the stats page. Pass `reading_cefr` to avoid
    a re-entrant call into proficiency.estimate()."""
    prog = level_progress()
    so = speaking_ord(prog)
    if reading_cefr is None:
        import proficiency
        reading_cefr = proficiency.estimate()["cefr"]
    rcefr = reading_cefr
    ro = cefr_ord(rcefr)
    # the words to work on next: commonest not-yet-touched in the band the
    # learner is currently in, then the next band up
    cur_i = min(len(prog) - 1, int(so))
    nxt = list(prog[cur_i]["next"])
    if len(nxt) < 15 and cur_i + 1 < len(prog):
        nxt += prog[cur_i + 1]["next"]
    return {
        "cefr": _ord_label(so),
        "ord": so,
        "reading_cefr": rcefr,
        "reading_ord": ro,
        "gap": round(ro - so, 2),
        "levels": prog,
        "next_words": nxt[:15],
        "own_threshold": _OWN,
    }
