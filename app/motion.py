"""Verbs-of-motion drill — a focused sibling of the grammar drill.

A card's front is a short English scene with ONE clause highlighted (subject +
motion verb + direction/location); the learner translates just that clause,
picking the right verb of motion, aspect/directionality, prefix and
preposition+case. Every card is tagged on five dimensions (`dims`) so progress
can be sliced by verb, aspect, prefix, preposition or tense — and the specific
combinations you keep missing come back as spaced re-tests.

Cards live in `motion_items`; misses in `motion_lapse`. Rolling LLM generation,
FSRS-free self-grading, promote-to-production, "practise this" bursts — the same
machinery as `drill.py`, just its own taxonomy.
"""
import json
import random
import re
import threading

import llm
import srs
import store

# ---------------------------------------------------------------- taxonomy

# id, unidirectional, multidirectional, English
VERBS = [
    ("idti", "идти", "ходить", "go on foot"),
    ("ehat", "ехать", "ездить", "go by vehicle"),
    ("bezhat", "бежать", "бегать", "run"),
    ("letet", "лететь", "летать", "fly"),
    ("plyt", "плыть", "плавать", "swim / sail"),
    ("nesti", "нести", "носить", "carry (in the hands / wear)"),
    ("vesti", "вести", "водить", "lead / drive a car"),
    ("vezti", "везти", "возить", "transport / give a lift"),
    ("polzti", "ползти", "ползать", "crawl"),
    ("lezt", "лезть", "лазить", "climb / clamber"),
    ("tashchit", "тащить", "таскать", "drag / lug"),
    ("katit", "катить", "катать", "roll / wheel"),
    ("gnat", "гнать", "гонять", "drive / chase"),
    ("bresti", "брести", "бродить", "trudge / wander"),
]
_VERB = {v[0]: v for v in VERBS}
# reverse lookup — the LLM sometimes echoes the Russian pair back instead of the id
_VERB_BY_TEXT = {}
for _v in VERBS:
    _VERB_BY_TEXT[_v[1]] = _v[0]
    _VERB_BY_TEXT[_v[2]] = _v[0]
    _VERB_BY_TEXT[f"{_v[1]} / {_v[2]}"] = _v[0]

ASPECTS = [
    ("uni", "one trip, in progress, one direction right now (иду / несу)"),
    ("multi_hab", "repeated / habitual trips (хожу каждый день, ходил в детстве)"),
    ("multi_round", "a completed there-and-back trip (вчера ходил в кино = went and came back)"),
    ("setoff", "пойти / поехать — set off / 'let's go' / the plain past 'went'"),
    ("pf_prefix", "prefixed perfective — one completed movement (пришёл, вошёл, уехал)"),
    ("impf_prefix", "prefixed imperfective — repeated / in progress / general (приходит, входил)"),
]
_ASPECT = dict(ASPECTS)

PREFIXES = [
    ("none", "", "no prefix — bare идти / ходить etc."),
    ("po", "по-", "set off (пойти, поехать)"),
    ("pri", "при-", "arrive, come, get here"),
    ("u", "у-", "leave, go away, be gone"),
    ("v", "в(о)-", "go in, enter"),
    ("vy", "вы-", "go out, step out (for a bit)"),
    ("pod", "под(о)-", "come up to, approach"),
    ("ot", "от(о)-", "move away from, step back"),
    ("za", "за-", "drop in / call by (за to a person or place); or go behind (за угол)"),
    ("do", "до-", "reach, get all the way to"),
    ("pro", "про-", "go past, through, or cover a distance"),
    ("pere", "пере-", "cross to the other side, or move (house)"),
    ("ob", "об(о)-", "go around, skirt, do a circuit of"),
    ("vz", "вз(о)-", "go up, ascend (взойти, взбежать)"),
    ("s", "с(о)-", "go / come down, get off (сойти, съехать)"),
    ("na", "на-", "onto a surface, or into something by accident (наехать на камень, налететь на столб)"),
    ("s_sya", "с(о)-…-ся", "come together, gather (собраться, съехаться)"),
    ("raz_sya", "раз(о)-…-ся", "disperse, scatter, go separate ways (разойтись, разъехаться)"),
]
_PREFIX = {p[0]: p for p in PREFIXES}

PREPS = [
    ("v_acc", "в + accusative", "into an enclosed space (в комнату, в дом)"),
    ("na_acc", "на + accusative", "to an open place / event / activity (на кухню, на работу, на концерт)"),
    ("k_dat", "к + dative", "up to a person or an edge (к врачу, к окну)"),
    ("iz_gen", "из + genitive", "out of an enclosed space (из комнаты)"),
    ("s_gen", "с + genitive", "off a surface / back from an activity (со стола, с работы)"),
    ("ot_gen", "от + genitive", "away from a person or point (от двери, от меня)"),
    ("do_gen", "до + genitive", "as far as (до угла, до озера)"),
    ("za_acc", "за + accusative", "behind / round the far side of (за угол, за дом)"),
    ("cherez_acc", "через + accusative", "across / through (через дорогу, через мост)"),
    ("po_dat", "по + dative", "along / around a surface (по улице, по парку)"),
    ("mimo_gen", "мимо + genitive", "past (мимо дома)"),
    ("vokrug_gen", "вокруг + genitive", "around (вокруг озера)"),
    ("domoy", "домой / сюда / туда (adverb)", "a direction adverb — no preposition"),
    ("none", "no preposition", "the direction is built into the verb / an adverb"),
]
_PREP = {p[0]: p for p in PREPS}

# Which prepositions naturally pair with a given prefix — used to bias card
# generation so scenes stay coherent ("вышел из комнаты", not "вышел в комнату").
# Not a hard filter; if the intersection with a level's preps is empty, anything
# the level allows is fair game.
_PREFIX_PREPS = {
    "none": ["v_acc", "na_acc", "k_dat", "iz_gen", "s_gen", "domoy", "none"],
    "po": ["v_acc", "na_acc", "k_dat", "domoy"],
    "pri": ["v_acc", "na_acc", "k_dat", "domoy"],
    "u": ["iz_gen", "s_gen", "ot_gen"],
    "v": ["v_acc", "na_acc"],
    "vy": ["iz_gen", "s_gen", "na_acc"],
    "pod": ["k_dat"],
    "ot": ["ot_gen"],
    "za": ["v_acc", "na_acc", "k_dat", "za_acc"],
    "do": ["do_gen"],
    "pro": ["cherez_acc", "mimo_gen", "po_dat"],
    "pere": ["cherez_acc", "v_acc"],
    "ob": ["vokrug_gen", "none"],
    "vz": ["na_acc", "none"],
    "s": ["s_gen", "iz_gen"],
    "na": ["na_acc"],
    "s_sya": ["k_dat", "v_acc", "none"],
    "raz_sya": ["po_dat", "none"],
}

TENSES = ["present", "past", "future", "imperative"]

# Difficulty levels — which combinations get generated at each. Deliberately
# fine-grained: each step adds ONE new thing to think about so confidence can
# build gradually. Prefixes get one dedicated rung per spatial pair, taught
# perfective-first, with all their imperfective twins turned on together at L10.
# `_level()` auto-nudges up/down on a rolling accuracy window.
_CORE5 = ["idti", "ehat", "bezhat", "letet", "plyt"]
_CORE8 = _CORE5 + ["nesti", "vesti", "vezti"]
_VERBS10 = [v[0] for v in VERBS[:10]]
_ALLV = [v[0] for v in VERBS]

LEVELS = {
    # 1 — just "one trip happening now" vs "every day", the two commonest verbs
    1: {"verbs": ["idti", "ehat"],
        "aspects": ["uni", "multi_hab"],
        "prefixes": ["none"],
        "preps": ["v_acc", "na_acc", "domoy"],
        "tenses": ["present"]},
    # 2 — + past tense, + к+dat / (from) home, + бежать
    2: {"verbs": ["idti", "ehat", "bezhat"],
        "aspects": ["uni", "multi_hab"],
        "prefixes": ["none"],
        "preps": ["v_acc", "na_acc", "k_dat", "domoy", "none"],
        "tenses": ["present", "past"]},
    # 3 — + пойти/поехать (set off / "went"), + future, + лететь
    3: {"verbs": ["idti", "ehat", "bezhat", "letet"],
        "aspects": ["uni", "multi_hab", "setoff"],
        "prefixes": ["none", "po"],
        "preps": ["v_acc", "na_acc", "k_dat", "domoy", "none"],
        "tenses": ["present", "past", "future"]},
    # 4 — + the there-and-back round trip (ходил = went and came back)
    4: {"verbs": _CORE5,
        "aspects": ["uni", "multi_hab", "multi_round", "setoff"],
        "prefixes": ["none", "po"],
        "preps": ["v_acc", "na_acc", "k_dat", "iz_gen", "s_gen", "domoy", "none"],
        "tenses": ["present", "past", "future"]},
    # 5 — prefix pair: при- / у- (arrive / leave), perfective only
    5: {"verbs": _CORE5 + ["nesti"],
        "aspects": ["uni", "multi_hab", "multi_round", "setoff", "pf_prefix"],
        "prefixes": ["none", "po", "pri", "u"],
        "preps": ["v_acc", "na_acc", "k_dat", "iz_gen", "s_gen", "ot_gen", "domoy", "none"],
        "tenses": ["present", "past", "future"]},
    # 6 — prefix pair: в- / вы- (go in / go out, step out)
    6: {"verbs": _CORE5 + ["nesti"],
        "aspects": ["uni", "multi_hab", "multi_round", "setoff", "pf_prefix"],
        "prefixes": ["none", "po", "pri", "u", "v", "vy"],
        "preps": ["v_acc", "na_acc", "k_dat", "iz_gen", "s_gen", "ot_gen", "domoy", "none"],
        "tenses": ["present", "past", "future"]},
    # 7 — prefix pair: под- / от- (approach / step back), + вести/везти
    7: {"verbs": _CORE8,
        "aspects": ["uni", "multi_hab", "multi_round", "setoff", "pf_prefix"],
        "prefixes": ["none", "po", "pri", "u", "v", "vy", "pod", "ot"],
        "preps": ["v_acc", "na_acc", "k_dat", "iz_gen", "s_gen", "ot_gen", "domoy", "none"],
        "tenses": ["present", "past", "future"]},
    # 8 — за- (drop by / go behind) and до- (reach all the way)
    8: {"verbs": _CORE8,
        "aspects": ["uni", "multi_hab", "multi_round", "setoff", "pf_prefix"],
        "prefixes": ["none", "po", "pri", "u", "v", "vy", "pod", "ot", "za", "do"],
        "preps": ["v_acc", "na_acc", "k_dat", "iz_gen", "s_gen", "ot_gen", "do_gen",
                  "za_acc", "domoy", "none"],
        "tenses": ["present", "past", "future"]},
    # 9 — про- (past / through) · пере- (across) · об- (around); route preps; commands
    9: {"verbs": _CORE8,
        "aspects": ["uni", "multi_hab", "multi_round", "setoff", "pf_prefix"],
        "prefixes": ["none", "po", "pri", "u", "v", "vy", "pod", "ot", "za", "do",
                     "pro", "pere", "ob"],
        "preps": ["v_acc", "na_acc", "k_dat", "iz_gen", "s_gen", "ot_gen", "do_gen",
                  "za_acc", "cherez_acc", "po_dat", "mimo_gen", "vokrug_gen", "domoy", "none"],
        "tenses": ["present", "past", "future", "imperative"]},
    # 10 — the imperfective twin of EVERY prefix so far (приходит, входил, заходит…)
    10: {"verbs": _CORE8,
         "aspects": [a[0] for a in ASPECTS],
         "prefixes": ["none", "po", "pri", "u", "v", "vy", "pod", "ot", "za", "do",
                      "pro", "pere", "ob"],
         "preps": ["v_acc", "na_acc", "k_dat", "iz_gen", "s_gen", "ot_gen", "do_gen",
                   "za_acc", "cherez_acc", "po_dat", "mimo_gen", "vokrug_gen", "domoy", "none"],
         "tenses": ["present", "past", "future", "imperative"]},
    # 11 — вз- (up) · с- (down / off) · на- (onto / into by accident)
    11: {"verbs": _VERBS10,
         "aspects": [a[0] for a in ASPECTS],
         "prefixes": ["none", "po", "pri", "u", "v", "vy", "pod", "ot", "za", "do",
                      "pro", "pere", "ob", "vz", "s", "na"],
         "preps": [p[0] for p in PREPS],
         "tenses": ["present", "past", "future", "imperative"]},
    # 12 — the whole inventory: reflexive gather/scatter + the rarer verbs
    12: {"verbs": _ALLV,
         "aspects": [a[0] for a in ASPECTS],
         "prefixes": [p[0] for p in PREFIXES],
         "preps": [p[0] for p in PREPS],
         "tenses": ["present", "past", "future", "imperative"]},
}
_MAX_LEVEL = max(LEVELS)

# one-line "what this rung is about", surfaced in the stats view
LEVEL_NOTES = {
    1: "one trip now vs. every day · present · идти / ехать",
    2: "+ past tense · + к someone · + бежать",
    3: "+ пойти / поехать (set off, “went”) · + future · + лететь",
    4: "+ the there-and-back round trip (ходил = went & came back)",
    5: "prefix pair при- / у- — arrive / leave (perfective)",
    6: "prefix pair в- / вы- — go in / step out",
    7: "prefix pair под- / от- — approach / move away · + вести, везти",
    8: "за- (drop by / go behind) · до- (reach all the way)",
    9: "про- past · пере- across · об- around · commands",
    10: "the imperfective twins — приходит, входил, заходил (repeated / ongoing)",
    11: "вз- up · с- down-off · на- onto / into by accident",
    12: "everything — собраться / разойтись, and ползти, лезть, тащить, катить, гнать, брести",
}

DIMS = ("verb", "aspect", "prefix", "prep", "tense")

_BATCH = 12
_MIN_BUFFER = 12
_TARGET_BUFFER = 30
_RETEST_GAP = (3, 5)
_RETEST_BATCH = 3
_PARKED = 1 << 30
_WINDOW = 24
_LEARN_MIN = 4
_LEARN_PCT = 0.8
_PRACTISE_MIN = 3

_gen_lock = threading.Lock()
_retest_lock = threading.Lock()


def _c():
    return store.connect()


def _level():
    try:
        return max(1, min(_MAX_LEVEL, int(srs.get_setting("motion_level", 1))))
    except (TypeError, ValueError):
        return 1


def _set_level(n):
    srs.set_setting("motion_level", max(1, min(_MAX_LEVEL, int(n))))


_ADJUST_WINDOW = 8        # recent distinct combos at this level to judge on
_ADJUST_UP = 0.85         # ≥ this share right → step up
_ADJUST_DOWN = 0.5        # ≤ this share right → step down


def _maybe_adjust_level():
    """After each graded card, nudge the level up on a strong run or down on a
    weak one — one step at a time. Judged on the most recent verdict for each of
    the last few DISTINCT combos at the current level, so one nightmare combo
    that keeps coming back as a re-test can't single-handedly crater the level."""
    lvl = _level()
    c = _c()
    rows = c.execute(
        "SELECT combo, verdict FROM motion_items "
        "WHERE verdict IS NOT NULL AND COALESCE(focus,0)=0 AND level = ? AND combo IS NOT NULL "
        "ORDER BY graded_at DESC, id DESC",
        (lvl,)).fetchall()
    c.close()
    latest = {}
    for r in rows:
        latest.setdefault(r["combo"], r["verdict"])
        if len(latest) >= _ADJUST_WINDOW:
            break
    if len(latest) < _ADJUST_WINDOW:
        return lvl
    pct = sum(1 for v in latest.values() if v == "right") / len(latest)
    if pct >= _ADJUST_UP and lvl < _MAX_LEVEL:
        _set_level(lvl + 1)
        return lvl + 1
    if pct <= _ADJUST_DOWN and lvl > 1:
        _set_level(lvl - 1)
        return lvl - 1
    return lvl


def _pos():
    try:
        return int(srs.get_setting("motion_pos", 0))
    except (TypeError, ValueError):
        return 0


def _bump_pos(by=1):
    srs.set_setting("motion_pos", _pos() + by)


# ---------------------------------------------------------------- dimension labels

def dim_values(dim):
    """[(value, label, help)] for a dimension, in catalog order."""
    if dim == "verb":
        return [(v[0], f"{v[1]} / {v[2]}", v[3]) for v in VERBS]
    if dim == "aspect":
        return [(a, a.replace("_", " "), h) for a, h in ASPECTS]
    if dim == "prefix":
        return [(p[0], p[1] or "(none)", p[2]) for p in PREFIXES]
    if dim == "prep":
        return [(p[0], p[1], p[2]) for p in PREPS]
    if dim == "tense":
        return [(t, t, "") for t in TENSES]
    return []


def _combo(dims):
    return "|".join(str(dims.get(d, "")) for d in DIMS)


def _combo_dims(combo):
    parts = (combo or "").split("|")
    return {d: (parts[i] if i < len(parts) else "") for i, d in enumerate(DIMS)}


# ---------------------------------------------------------------- selection

def _combo_stats_map():
    """{combo: {right, wrong}} over each combo's recent window."""
    c = _c()
    rows = c.execute(
        "SELECT combo, verdict FROM motion_items "
        "WHERE combo IS NOT NULL AND combo <> '' AND verdict IS NOT NULL "
        "ORDER BY graded_at DESC, id DESC").fetchall()
    c.close()
    agg = {}
    for r in rows:
        v = agg.setdefault(r["combo"], {"right": 0, "wrong": 0})
        if v["right"] + v["wrong"] >= _WINDOW:
            continue
        v["right" if r["verdict"] == "right" else "wrong"] += 1
    return agg


def pick_combos(n):
    lvl = _level()
    spec = LEVELS[lvl]
    stats = _combo_stats_map()
    out, seen = [], set()
    tries = 0
    while len(out) < n and tries < n * 12:
        tries += 1
        verb = random.choice(spec["verbs"])
        aspect = random.choice(spec["aspects"])
        # a bare (none) prefix pairs with uni/multi aspects; a prefixed aspect needs a real prefix
        if aspect in ("pf_prefix", "impf_prefix"):
            pfx = random.choice([p for p in spec["prefixes"] if p not in ("none", "po")] or ["pri"])
        elif aspect in ("uni", "multi_hab", "multi_round"):
            pfx = "none"
        else:  # setoff
            pfx = "po"
        # pick a preposition that actually goes with this prefix where possible
        pool = [p for p in spec["preps"] if p in _PREFIX_PREPS.get(pfx, spec["preps"])] or spec["preps"]
        prep = random.choice(pool)
        tense = random.choice(spec["tenses"])
        # keep combinations that actually occur in the language
        if aspect == "multi_round" and tense != "past":
            continue
        if aspect == "setoff" and tense == "present":
            continue           # пойти/поехать has no present ("иду" is the uni verb)
        if aspect == "pf_prefix" and tense == "present":
            continue           # a perfective has no present tense
        dims = {"verb": verb, "aspect": aspect, "prefix": pfx, "prep": prep, "tense": tense}
        sig = _combo(dims)
        if sig in seen:
            continue
        # weight: bias toward combos that are shaky or unseen
        st = stats.get(sig, {"right": 0, "wrong": 0})
        tot = st["right"] + st["wrong"]
        if tot == 0:
            w = 1.6
        elif tot >= _LEARN_MIN and st["right"] / tot >= _LEARN_PCT:
            w = 0.15
        elif st["right"] / max(1, tot) < 0.6:
            w = 2.5
        else:
            w = 1.0
        if random.random() > w / 2.5:
            continue
        seen.add(sig)
        out.append({**dims, "combo": sig,
                    "verb_pair": f"{_VERB[verb][1]} / {_VERB[verb][2]}",
                    "verb_en": _VERB[verb][3],
                    "aspect_help": _ASPECT[aspect],
                    "prefix_help": _PREFIX[pfx][2] if pfx in _PREFIX else "",
                    "prep_help": _PREP[prep][2] if prep in _PREP else ""})
    return out


# ---------------------------------------------------------------- generation

def _norm_dims(dims):
    d = {k: str((dims or {}).get(k, "")).strip() for k in DIMS}
    if d["verb"] and d["verb"] not in _VERB:
        d["verb"] = _VERB_BY_TEXT.get(d["verb"], d["verb"])
    return d


# the model sometimes "thinks out loud" in a field — realises the scene forces a
# different verb/aspect than it started with and patches it in the note instead of
# rewriting the card. Reject anything that reads like a self-correction.
_SLOP_NOTE = re.compile(
    r"(?i)(\bwait\b|^\s*(actually|hmm+|oops|correction|scratch|never ?mind|so actually|"
    r"on second|let me)\b|scratch that|on second thought|let me reconsider|"
    r"i meant\b|i mean\b|my (mistake|bad)|should (actually )?be\b|i should have|"
    r"str\.?\s|—\s*(actually|wait|no,)\b)")
_SLOP_SCENE = re.compile(r"(?i)(^\s*(actually|hmm+|oops|correction)\b|scratch that|"
                         r"on second thought|let me reconsider|i meant\b|— actually\b)")
_LAT2 = re.compile(r"[A-Za-z]{2,}")
_USE_RU = re.compile(r"(?i)\buse\s+([а-яёА-ЯЁ][а-яёА-ЯЁ-]+)")


def _looks_like_slop(situation, highlight, answer, note, contrast):
    if _SLOP_NOTE.search(note or "") or _SLOP_NOTE.search(contrast or ""):
        return True
    if _SLOP_SCENE.search(situation or "") or _SLOP_SCENE.search(highlight or ""):
        return True
    if _LAT2.search(answer or ""):                     # English leaked into the Russian answer
        return True
    # note says "use <russian form>" but that form isn't in the answer → self-corrected
    m = _USE_RU.search(note or "")
    if m and m.group(1).lower().replace("́", "") not in (answer or "").lower():
        return True
    return False


def _row(card, known=None):
    situation = (card.get("situation") or "").strip()
    highlight = (card.get("highlight") or "").strip()
    answer = (card.get("answer") or "").strip().replace("́", "")
    given = [str(g).strip().replace("́", "") for g in (card.get("given") or []) if str(g).strip()]
    tgt = [str(t).strip().replace("́", "") for t in (card.get("target") or []) if str(t).strip()]
    note = (card.get("note") or "").strip() or None
    contrast = (card.get("contrast") or "").strip() or None
    # trust the combo we asked for over whatever the model echoed back
    dims = _norm_dims(known) if known else _norm_dims(card.get("dims"))
    alts = []
    for a in (card.get("alts") or []):
        if isinstance(a, dict):
            form = str(a.get("form", "")).strip().replace("́", "")
            why = str(a.get("why", "")).strip()
        else:
            form, why = str(a).strip().replace("́", ""), ""
        if form:
            alts.append({"form": form, "why": why})
    ok = bool(situation and highlight and answer) and not _looks_like_slop(
        situation, highlight, answer, note, contrast)
    return {
        "ok": ok,
        "situation": situation, "highlight": highlight, "answer": answer,
        "given": json.dumps(given, ensure_ascii=False) if given else None,
        "target": json.dumps(tgt, ensure_ascii=False) if tgt else None,
        "note": note,
        "contrast": contrast,
        "alts": json.dumps(alts, ensure_ascii=False) if alts else None,
        "dims": json.dumps(dims, ensure_ascii=False),
        "combo": _combo(dims) if any(dims.values()) else (card.get("combo") or None),
    }


def _insert(cards, *, retest_for=None, focus=0, combos=None):
    c = _c()
    n = 0
    cards = cards or []
    match = combos if combos and len(combos) == len(cards) else None
    for i, card in enumerate(cards):
        f = _row(card, match[i] if match else None)
        if not f["ok"]:
            continue
        c.execute(
            """INSERT INTO motion_items(level, situation, highlight, given, answer, target,
                                        note, contrast, alts, dims, combo, retest_for, focus)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_level(), f["situation"], f["highlight"], f["given"], f["answer"], f["target"],
             f["note"], f["contrast"], f["alts"], f["dims"], f["combo"], retest_for, focus))
        n += 1
    c.commit()
    c.close()
    return n


def _generate_batch():
    combos = pick_combos(_BATCH)
    if not combos:
        return 0
    try:
        out = llm.motion_cards(combos, level=_level())
    except Exception as e:  # noqa: BLE001
        print(f"[motion] generation failed: {e}", flush=True)
        return 0
    return _insert(out.get("cards"), combos=combos)


def _servable_count(c):
    return c.execute(
        "SELECT COUNT(*) n FROM motion_items WHERE verdict IS NULL AND retest_for IS NULL "
        "AND COALESCE(focus,0)=0 "
        "AND (served_at IS NULL OR served_at < datetime('now','-30 minutes'))").fetchone()["n"]


def _topup(force=False):
    if not _gen_lock.acquire(blocking=False):
        return
    try:
        c = _c()
        have = _servable_count(c)
        c.close()
        rounds = 0
        while (force or have < _TARGET_BUFFER) and rounds < 3:
            made = _generate_batch()
            if not made:
                break
            have += made
            rounds += 1
            force = False
    finally:
        _gen_lock.release()


def topup_async(force=False):
    threading.Thread(target=_topup, kwargs={"force": force}, daemon=True).start()


# ---------------------------------------------------------------- lapses / re-tests

def _lapse_miss(combo, item_id):
    if not combo:
        return
    due = _pos() + random.randint(*_RETEST_GAP)
    c = _c()
    c.execute("INSERT OR IGNORE INTO motion_lapse(combo) VALUES(?)", (combo,))
    c.execute("UPDATE motion_lapse SET misses = misses + 1, resolved = 0, due_pos = ?, "
              "src_item_id = COALESCE(src_item_id, ?) WHERE combo = ?", (due, item_id, combo))
    row = c.execute("SELECT id, misses FROM motion_lapse WHERE combo = ?", (combo,)).fetchone()
    c.commit()
    c.close()
    if row and row["misses"] >= 2:
        _gen_retest_async(row["id"])


def _lapse_clear(combo):
    if not combo:
        return
    c = _c()
    c.execute("UPDATE motion_lapse SET resolved = 1 WHERE resolved = 0 AND combo = ?", (combo,))
    c.execute("DELETE FROM motion_items WHERE retest_for IN "
              "(SELECT id FROM motion_lapse WHERE resolved = 1) AND verdict IS NULL")
    c.commit()
    c.close()


def _retest_graded(lapse_id, verdict):
    c = _c()
    lap = c.execute("SELECT * FROM motion_lapse WHERE id = ?", (lapse_id,)).fetchone()
    if not lap:
        c.close()
        return
    if verdict == "right":
        c.execute("UPDATE motion_lapse SET resolved = 1 WHERE id = ?", (lapse_id,))
        c.execute("DELETE FROM motion_items WHERE retest_for = ? AND verdict IS NULL", (lapse_id,))
        c.commit()
        c.close()
        return
    due = _pos() + random.randint(*_RETEST_GAP)
    c.execute("UPDATE motion_lapse SET misses = misses + 1, due_pos = ? WHERE id = ?",
              (due, lapse_id))
    c.commit()
    c.close()
    _gen_retest_async(lapse_id)


def _gen_retest(lapse_id):
    c = _c()
    lap = c.execute("SELECT * FROM motion_lapse WHERE id = ? AND resolved = 0",
                    (lapse_id,)).fetchone()
    if not lap:
        c.close()
        return 0
    have = c.execute("SELECT COUNT(*) n FROM motion_items WHERE retest_for = ? AND verdict IS NULL",
                     (lapse_id,)).fetchone()["n"]
    combo = lap["combo"]
    c.close()
    if have >= _RETEST_BATCH or not combo:
        return 0
    dims = _combo_dims(combo)
    spec = {**dims, "combo": combo, "verb_pair": _combo_verb_pair(dims),
            "aspect_help": _ASPECT.get(dims["aspect"], ""),
            "prefix_help": _PREFIX.get(dims["prefix"], ("", "", ""))[2],
            "prep_help": _PREP.get(dims["prep"], ("", "", ""))[2]}
    try:
        out = llm.motion_focus_cards(spec, n=_RETEST_BATCH, level=_level())
    except Exception as e:  # noqa: BLE001
        print(f"[motion] retest gen failed: {e}", flush=True)
        return 0
    cards = out.get("cards") or []
    return _insert(cards, retest_for=lapse_id, combos=[dims] * len(cards))


def _combo_verb_pair(dims):
    v = _VERB.get(dims.get("verb"))
    return f"{v[1]} / {v[2]}" if v else dims.get("verb", "")


def _gen_retest_async(lapse_id):
    def run():
        if not _retest_lock.acquire(blocking=False):
            return
        try:
            _gen_retest(lapse_id)
        finally:
            _retest_lock.release()
    threading.Thread(target=run, daemon=True).start()


def _due_retests(c, need):
    if need <= 0:
        return []
    pos = _pos()
    laps = c.execute(
        "SELECT * FROM motion_lapse WHERE resolved = 0 AND due_pos > 0 AND due_pos <= ? "
        "ORDER BY misses DESC, due_pos ASC LIMIT ?", (pos, need)).fetchall()
    out = []
    for lap in laps:
        item = None
        if lap["misses"] >= 2:
            r = c.execute("SELECT * FROM motion_items WHERE retest_for = ? AND verdict IS NULL "
                          "AND served_at IS NULL ORDER BY id LIMIT 1", (lap["id"],)).fetchone()
            if r:
                c.execute("UPDATE motion_items SET served_at = datetime('now') WHERE id = ?",
                          (r["id"],))
                item = dict(r)
            else:
                _gen_retest_async(lap["id"])
                continue
        else:
            src = c.execute("SELECT * FROM motion_items WHERE id = ?",
                            (lap["src_item_id"],)).fetchone()
            if not src:
                c.execute("UPDATE motion_lapse SET resolved = 1 WHERE id = ?", (lap["id"],))
                continue
            cur = c.execute(
                """INSERT INTO motion_items(level, situation, highlight, given, answer, target,
                                            note, contrast, alts, dims, combo, retest_for, served_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
                (src["level"], src["situation"], src["highlight"], src["given"], src["answer"],
                 src["target"], src["note"], src["contrast"], src["alts"], src["dims"],
                 src["combo"], lap["id"]))
            item = dict(c.execute("SELECT * FROM motion_items WHERE id = ?",
                                  (cur.lastrowid,)).fetchone())
        c.execute("UPDATE motion_lapse SET due_pos = ? WHERE id = ?", (_PARKED, lap["id"]))
        out.append({**_public(item), "retest": True})
    return out


# ---------------------------------------------------------------- serve / grade

def _jsonlist(r, col):
    try:
        v = json.loads(r[col]) if r.get(col) else []
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _public(r):
    dims = {}
    try:
        dims = json.loads(r["dims"]) if r.get("dims") else {}
    except (ValueError, TypeError):
        dims = {}
    verb = _VERB.get(dims.get("verb"))
    try:
        alts = json.loads(r["alts"]) if r.get("alts") else []
        alts = [a for a in alts if isinstance(a, dict) and a.get("form")]
    except (ValueError, TypeError):
        alts = []
    return {
        "id": r["id"], "situation": r["situation"], "highlight": r["highlight"],
        "given": _jsonlist(r, "given"), "answer": r["answer"], "target": _jsonlist(r, "target"),
        "note": r["note"], "contrast": r.get("contrast"), "alts": alts,
        "dims": dims, "combo": r.get("combo"),
        "verb_pair": f"{verb[1]} / {verb[2]}" if verb else None,
        "retest": bool(r.get("retest_for")), "focus": bool(r.get("focus")),
        "card_id": r["card_id"], "level": r["level"],
    }


def state():
    c = _c()
    unseen = _servable_count(c)
    seen = c.execute("SELECT COUNT(*) n FROM motion_items WHERE verdict IS NOT NULL").fetchone()["n"]
    right = c.execute("SELECT COUNT(*) n FROM motion_items WHERE verdict='right'").fetchone()["n"]
    lapses = c.execute("SELECT COUNT(*) n FROM motion_lapse WHERE resolved=0").fetchone()["n"]
    c.close()
    lvl = _level()
    return {"buffer": unseen, "seen": seen, "right": right, "open_lapses": lapses,
            "level": lvl, "max_level": _MAX_LEVEL, "level_note": LEVEL_NOTES.get(lvl, "")}


def next_items(n=1):
    n = max(1, n)
    c = _c()
    # re-tests never take more than half a pull — otherwise a pile of open lapses
    # starves out fresh material and you can never leave the combos you're stuck on
    retest_cap = max(1, n // 2)
    # ...but every ~3rd single card served is a re-test, so they still recur
    if n == 1 and _pos() % 3 != 0:
        retest_cap = 0
    out = _due_retests(c, retest_cap) if retest_cap else []
    rows = []
    if len(out) < n:
        rows = c.execute(
            """SELECT * FROM motion_items
               WHERE verdict IS NULL AND (
                     (COALESCE(focus,0)=1 AND served_at IS NULL)
                     OR (retest_for IS NULL AND COALESCE(focus,0)=0
                         AND (served_at IS NULL OR served_at < datetime('now','-30 minutes'))))
               ORDER BY COALESCE(focus,0) DESC, served_at IS NULL DESC, id
               LIMIT ?""", (n - len(out),)).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            c.execute(f"UPDATE motion_items SET served_at=datetime('now') "
                      f"WHERE id IN ({','.join('?' * len(ids))})", ids)
            c.commit()
    left = _servable_count(c)
    c.close()
    out += [_public(dict(r)) for r in rows]
    if out:
        _bump_pos(len(out))
        prewarm_refs_async(
            verb_ids=[o["dims"].get("verb") for o in out if o.get("dims")],
            prefix_ids=[o["dims"].get("prefix") for o in out if o.get("dims")])
    if left < _MIN_BUFFER:
        topup_async()
    return out


def grade(item_id, verdict):
    verdict = "right" if verdict == "right" else "wrong"
    c = _c()
    r = c.execute("SELECT verdict, combo, retest_for FROM motion_items WHERE id=?",
                  (item_id,)).fetchone()
    if not r:
        c.close()
        return None
    fresh = r["verdict"] is None
    combo, retest_for = r["combo"], r["retest_for"]
    c.execute("UPDATE motion_items SET verdict=?, graded_at=datetime('now') WHERE id=?",
              (verdict, item_id))
    c.commit()
    c.close()
    if not fresh:
        return {**state()}
    if retest_for:
        _retest_graded(retest_for, verdict)
        return {"retest": True, **state()}
    if verdict == "wrong":
        _lapse_miss(combo, item_id)
    else:
        _lapse_clear(combo)
    before = _level()
    after = _maybe_adjust_level()
    st = state()
    if after != before:
        st["level_changed"] = after
        topup_async(force=True)          # regenerate the buffer at the new level
    return st


# ---------------------------------------------------------------- "practise this"

def learn(dim, value, n=6):
    """Queue a burst of focus cards for one dimension value (e.g. verb=nesti, or
    prefix=pere). Combos are filled in around it at the current level."""
    if dim not in DIMS:
        return 0
    spec = LEVELS[_level()]
    made_specs = []
    for _ in range(n):
        d = {"verb": random.choice(spec["verbs"]),
             "aspect": random.choice(spec["aspects"]),
             "prefix": "none", "prep": random.choice(spec["preps"]),
             "tense": random.choice(spec["tenses"])}
        d[dim] = value
        if d["aspect"] in ("pf_prefix", "impf_prefix") and d["prefix"] == "none" and dim != "prefix":
            d["prefix"] = random.choice([p for p in spec["prefixes"] if p != "none"] or ["pri"])
        if d["aspect"] in ("uni", "multi_hab", "multi_round") and dim != "prefix":
            d["prefix"] = "none"
        d["combo"] = _combo(d)
        d.update(verb_pair=_combo_verb_pair(d), aspect_help=_ASPECT.get(d["aspect"], ""),
                 prefix_help=_PREFIX.get(d["prefix"], ("", "", ""))[2],
                 prep_help=_PREP.get(d["prep"], ("", "", ""))[2])
        made_specs.append(d)
    try:
        out = llm.motion_focus_cards(made_specs, n=n, level=_level(), dim_hint=(dim, value))
    except Exception as e:  # noqa: BLE001
        print(f"[motion] learn gen failed: {e}", flush=True)
        return 0
    return _insert(out.get("cards"), focus=1, combos=made_specs)


# ---------------------------------------------------------------- stats

def _status(right, wrong):
    n = right + wrong
    pct = round(right / n, 2) if n else 0.0
    if n == 0:
        s = "new"
    elif n < _PRACTISE_MIN:
        s = "seen"
    elif n >= _LEARN_MIN and pct >= _LEARN_PCT:
        s = "learned"
    else:
        s = "practising"
    return {"seen": n, "right": right, "wrong": wrong, "pct": pct, "status": s}


def slice_stats(dim):
    """Per value of `dim`: right/wrong/status over the recent window."""
    if dim not in DIMS:
        return []
    idx = DIMS.index(dim)
    c = _c()
    rows = c.execute(
        "SELECT combo, verdict FROM motion_items "
        "WHERE combo IS NOT NULL AND verdict IS NOT NULL "
        "ORDER BY graded_at DESC, id DESC").fetchall()
    c.close()
    agg = {}
    for r in rows:
        parts = (r["combo"] or "").split("|")
        if idx >= len(parts) or not parts[idx]:
            continue
        v = agg.setdefault(parts[idx], {"right": 0, "wrong": 0})
        if v["right"] + v["wrong"] >= _WINDOW:
            continue
        v["right" if r["verdict"] == "right" else "wrong"] += 1
    out = []
    for value, label, help_ in dim_values(dim):
        a = agg.get(value, {"right": 0, "wrong": 0})
        out.append({"value": value, "label": label, "help": help_, **_status(a["right"], a["wrong"])})
    return out


def combo_stats(limit=200):
    """The specific combinations you've been tested on, worst-first."""
    c = _c()
    rows = c.execute(
        "SELECT combo, verdict FROM motion_items WHERE combo IS NOT NULL AND verdict IS NOT NULL "
        "ORDER BY graded_at DESC, id DESC").fetchall()
    c.close()
    agg = {}
    for r in rows:
        v = agg.setdefault(r["combo"], {"right": 0, "wrong": 0})
        if v["right"] + v["wrong"] >= _WINDOW:
            continue
        v["right" if r["verdict"] == "right" else "wrong"] += 1
    out = []
    for combo, a in agg.items():
        d = _combo_dims(combo)
        verb = _VERB.get(d["verb"])
        out.append({
            "combo": combo,
            "label": (f"{verb[1]}/{verb[2]}" if verb else d["verb"]) + " · "
                     + d["aspect"].replace("_", " ")
                     + (" · " + _PREFIX[d["prefix"]][1] if d.get("prefix") not in ("", "none") and d["prefix"] in _PREFIX else "")
                     + (" · " + _PREP[d["prep"]][1] if d.get("prep") not in ("", "none") and d["prep"] in _PREP else "")
                     + " · " + d["tense"],
            "dims": d, **_status(a["right"], a["wrong"])})
    out.sort(key=lambda x: (x["pct"], -x["seen"]))
    return out[:limit]


def overall():
    c = _c()
    row = c.execute("SELECT COUNT(*) n, SUM(verdict='right') r FROM motion_items "
                    "WHERE verdict IS NOT NULL").fetchone()
    c.close()
    n = row["n"] or 0
    return {"cards": n, "right": row["r"] or 0, "pct": round((row["r"] or 0) / n, 2) if n else 0.0}


def full_stats():
    lvl = _level()
    return {
        "overall": overall(),
        "level": lvl,
        "max_level": _MAX_LEVEL,
        "level_note": LEVEL_NOTES.get(lvl, ""),
        "levels": [{"n": n, "note": LEVEL_NOTES.get(n, "")} for n in sorted(LEVELS)],
        "slices": {d: slice_stats(d) for d in DIMS},
        "combos": combo_stats(),
        "open_lapses": state()["open_lapses"],
    }


# ---------------------------------------------------------------- verb reference

_ref_lock = threading.Lock()

def _pfx_stem(label):
    """Reduce a prefix label to its bare consonant stem: «в(о)-» → «в», «подо-» →
    «под», «с(о)-…-ся» → «с»."""
    s = (label or "").replace("…", "").replace("ся", "").replace("(о)", "о")
    s = re.sub(r"[()\-\s]", "", s)
    return re.sub(r"о$", "", s)   # drop the fill vowel: подо → под, во → в


# bare-stem → id (spatial prefixes); reflexive stems tie-break in _prefix_id_from_label
_PFX_STEM = {}
for _p in PREFIXES:
    if _p[1] and _p[0] not in ("s_sya", "raz_sya"):
        _PFX_STEM.setdefault(_pfx_stem(_p[1]), _p[0])
# prefixed-infinitive prefix (no fill-vowel drop) → id, longest-first
_PFX_INF = sorted(
    ((re.sub(r"[()\-…\s]", "", _p[1]).replace("ся", ""), _p[0]) for _p in PREFIXES if _p[1]),
    key=lambda kv: -len(kv[0]))


def _prefix_id_from_label(label):
    lab = (label or "")
    if "ся" in lab:
        return "raz_sya" if "раз" in lab else "s_sya"
    for part in re.split(r"[/,]", lab):
        st = _pfx_stem(part)
        if st and st in _PFX_STEM:
            return _PFX_STEM[st]
    return None


def _prefix_id_from_form(form):
    """Best-effort: does a prefixed infinitive like «подойти» start with a prefix?"""
    f = (form or "").lower().strip()
    for st, pid in _PFX_INF:
        if st and f.startswith(st) and len(f) > len(st) + 1:
            return pid
    return None


def _clean_ref(data, v):
    """Normalise the LLM reference; strip stress marks."""
    def _s(x):
        return str(x or "").replace("́", "").strip()

    def _pair(x):
        if isinstance(x, (list, tuple)):
            return [_s(x[0]) if len(x) > 0 else "", _s(x[1]) if len(x) > 1 else ""]
        return [_s(x), ""]

    conj = data.get("conj") or {}
    out_conj = {}
    for slot in ("present", "past", "imperative"):
        rows = conj.get(slot) or {}
        if isinstance(rows, dict):
            out_conj[slot] = {_s(k): _pair(val) for k, val in rows.items() if _s(k)}
    out_conj["future_note"] = _s(conj.get("future_note"))
    mistakes = _mistakes(data)
    examples = _examples(data)
    prefixes = []
    for pr in (data.get("prefixes") or [])[:12]:
        if isinstance(pr, dict) and (pr.get("prefix") or pr.get("pf")):
            lab = _s(pr.get("prefix"))
            prefixes.append({
                "prefix": lab,
                "prefix_id": _prefix_id_from_label(lab) or _prefix_id_from_form(_s(pr.get("pf"))),
                "pf": _s(pr.get("pf")), "impf": _s(pr.get("impf")),
                "note": _s(pr.get("note"))})
    return {
        "verb_id": v[0], "uni": v[1], "multi": v[2], "en": v[3],
        "summary": _s(data.get("summary")),
        "uni_use": _s(data.get("uni_use")), "multi_use": _s(data.get("multi_use")),
        "conj": out_conj, "prefixes": prefixes,
        "mistakes": mistakes, "examples": examples,
    }


def _mistakes(data):
    out = []
    for m in (data.get("mistakes") or [])[:5]:
        if isinstance(m, dict) and (m.get("wrong") or m.get("right")):
            out.append({"wrong": str(m.get("wrong") or "").replace("́", "").strip(),
                        "right": str(m.get("right") or "").replace("́", "").strip(),
                        "why": str(m.get("why") or "").strip()})
    return out


def _examples(data):
    out = []
    for e in (data.get("examples") or [])[:8]:
        if isinstance(e, dict) and e.get("ru"):
            out.append({"ru": str(e.get("ru") or "").replace("́", "").strip(),
                        "en": str(e.get("en") or "").strip()})
    return out


def verb_reference(verb_id, generate=True):
    """The cached reference page for a verb pair, generating + storing it on the
    first request. Returns None for an unknown verb or a generation failure."""
    v = _VERB.get(verb_id)
    if not v:
        return None
    c = _c()
    row = c.execute("SELECT data FROM motion_verb_ref WHERE verb_id=?", (verb_id,)).fetchone()
    c.close()
    if row:
        try:
            return json.loads(row["data"])
        except (ValueError, TypeError):
            pass
    if not generate:
        return None
    try:
        raw = llm.motion_verb_reference(v[1], v[2], v[3])
    except Exception as e:  # noqa: BLE001
        print(f"[motion] verb-ref gen failed for {verb_id}: {e}", flush=True)
        return None
    data = _clean_ref(raw, v)
    c = _c()
    c.execute("INSERT OR REPLACE INTO motion_verb_ref(verb_id, data, created_at) "
              "VALUES(?,?, datetime('now'))", (verb_id, json.dumps(data, ensure_ascii=False)))
    c.commit()
    c.close()
    return data


def _clean_prefix_ref(data, p):
    return {
        "prefix_id": p[0], "prefix": p[1], "meaning": p[2],
        "what_it_does": str(data.get("what_it_does") or "").replace("́", "").strip(),
        "usage": str(data.get("usage") or "").replace("́", "").strip(),
        "exceptions": [
            {"verb": str(x.get("verb") or "").replace("́", "").strip(),
             "meaning": str(x.get("meaning") or "").strip(),
             "example": str(x.get("example") or "").replace("́", "").strip()}
            for x in (data.get("exceptions") or [])[:5]
            if isinstance(x, dict) and x.get("verb")],
        "mistakes": _mistakes(data), "examples": _examples(data),
    }


def prefix_reference(prefix_id, generate=True):
    """The cached reference page for a motion prefix (не 'none')."""
    p = _PREFIX.get(prefix_id)
    if not p or prefix_id == "none":
        return None
    c = _c()
    row = c.execute("SELECT data FROM motion_prefix_ref WHERE prefix_id=?",
                    (prefix_id,)).fetchone()
    c.close()
    if row:
        try:
            return json.loads(row["data"])
        except (ValueError, TypeError):
            pass
    if not generate:
        return None
    try:
        raw = llm.motion_prefix_reference(p[1], p[2])
    except Exception as e:  # noqa: BLE001
        print(f"[motion] prefix-ref gen failed for {prefix_id}: {e}", flush=True)
        return None
    data = _clean_prefix_ref(raw, p)
    c = _c()
    c.execute("INSERT OR REPLACE INTO motion_prefix_ref(prefix_id, data, created_at) "
              "VALUES(?,?, datetime('now'))", (prefix_id, json.dumps(data, ensure_ascii=False)))
    c.commit()
    c.close()
    return data


def prewarm_refs_async(verb_ids=(), prefix_ids=()):
    """Generate any missing verb / prefix references in the background so a tap on
    the card back lands on a cache hit."""
    vids = [x for x in dict.fromkeys(verb_ids) if x in _VERB]
    pids = [x for x in dict.fromkeys(prefix_ids) if x in _PREFIX and x != "none"]
    if not vids and not pids:
        return

    def run():
        if not _ref_lock.acquire(blocking=False):
            return
        try:
            c = _c()
            hv = {r["verb_id"] for r in c.execute("SELECT verb_id FROM motion_verb_ref")}
            hp = {r["prefix_id"] for r in c.execute("SELECT prefix_id FROM motion_prefix_ref")}
            c.close()
            for vid in vids:
                if vid not in hv:
                    verb_reference(vid)
            for pid in pids:
                if pid not in hp:
                    prefix_reference(pid)
        finally:
            _ref_lock.release()
    threading.Thread(target=run, daemon=True).start()


# backwards-compat alias
def prewarm_verb_refs_async(verb_ids):
    prewarm_refs_async(verb_ids=verb_ids)


# ---------------------------------------------------------------- save as card

def save_as_card(item_id):
    c = _c()
    r = c.execute("SELECT * FROM motion_items WHERE id=?", (item_id,)).fetchone()
    if not r:
        c.close()
        return None
    r = dict(r)
    c.close()
    if r["card_id"]:
        return srs.get_card(r["card_id"])
    p = _public(r)
    front = p["highlight"] + (f"\n({p['situation']})" if p["situation"] != p["highlight"] else "")
    meta = {"kind": "motion", "given": p["given"], "target": p["target"],
            "contrast": r["contrast"], "alts": p["alts"], "dims": p["dims"]}
    card = srs.create_production_card(front, r["answer"], note=r["note"],
                                     speak_ref=f"motion:{item_id}", meta=meta)
    c = _c()
    c.execute("UPDATE motion_items SET card_id=? WHERE id=?", (card["id"], item_id))
    c.commit()
    c.close()
    return card


def session_suggest(item_ids, limit=8):
    ids = [int(x) for x in item_ids if str(x).strip().lstrip("-").isdigit()]
    if not ids:
        return []
    c = _c()
    rows = c.execute(
        f"SELECT * FROM motion_items WHERE id IN ({','.join('?' * len(ids))}) AND verdict='wrong'",
        ids).fetchall()
    misses = {l["combo"]: l["misses"]
              for l in c.execute("SELECT combo, misses FROM motion_lapse").fetchall()}
    c.close()
    best = {}
    for r in rows:
        p = _public(dict(r))
        key = r["combo"]
        m = misses.get(key, 1)
        if key not in best or m > best[key]["misses"]:
            best[key] = {**p, "misses": m, "already_card": bool(r["card_id"])}
    out = sorted(best.values(), key=lambda x: -x["misses"])
    return out[:max(1, limit)]
