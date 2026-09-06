"""Russian stress marking for generated reading text.

The learner reads flow-reading stories with stress marks on every multi-syllable
word — they know stress for common words but not for every inflected form, and
that is exactly where a dictionary shines.

Pipeline:
  1. DICTIONARY (mark_text) — a 3.2M-wordform stress dictionary compiled from
     Russian Wiktionary (bundled from the ruaccent project, `app/data/stress/`).
     Loaded once into a local SQLite (`app/data/stress.db`, built on first use,
     not backed up, regenerable). Gets the mechanical inflected stress right.
  2. LLM PASS (accent_paragraphs) — resolves the two things the dictionary can't:
     homographs (за́мок / замо́к — the dict stores one default per form; ~20k
     forms are flagged ambiguous) and out-of-vocabulary words. Then a second
     verification pass re-checks every mark against the sentence.

Marks are U+0301 (combining acute) after the stressed vowel. ё is left as-is
(inherently stressed). One-syllable words get no mark.
"""
import gzip
import json
import os
import re
import sqlite3
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import llm  # noqa: E402

_DATA = os.path.join(os.path.dirname(__file__), "data", "stress")
_DB = os.path.join(os.path.dirname(__file__), "data", "stress.db")
_ACUTE = "́"
_VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
_CYR_WORD = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё́-]*")
_lock = threading.Lock()
_conn = None


# ---------------------------------------------------------------- dictionary db

def _build_db():
    tmp = _DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.execute("PRAGMA journal_mode=OFF")
    c.execute("PRAGMA synchronous=OFF")
    c.execute("CREATE TABLE stress(form TEXT PRIMARY KEY, accented TEXT) WITHOUT ROWID")
    c.execute("CREATE TABLE omograph(form TEXT PRIMARY KEY) WITHOUT ROWID")
    with gzip.open(os.path.join(_DATA, "accents.json.gz")) as f:
        d = json.load(f)
    c.executemany("INSERT OR IGNORE INTO stress VALUES(?,?)", d.items())
    with gzip.open(os.path.join(_DATA, "omographs.json.gz")) as f:
        om = json.load(f)
    c.executemany("INSERT OR IGNORE INTO omograph VALUES(?)", ((k,) for k in om))
    c.commit()
    c.close()
    os.replace(tmp, _DB)


def _db():
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        need = not os.path.exists(_DB)
        if not need:
            try:
                src = os.path.getmtime(os.path.join(_DATA, "accents.json.gz"))
                need = os.path.getmtime(_DB) < src
            except OSError:
                need = True
        if need:
            _build_db()
        _conn = sqlite3.connect(_DB, check_same_thread=False)
    return _conn


def _syllables(w):
    return sum(1 for ch in w if ch in _VOWELS)


def _apply(orig_word, dict_val):
    """Put the stress from `dict_val` (e.g. 'раб+отали') onto `orig_word`, which
    may differ only in case / surrounding form. `+` sits just before the stressed
    vowel."""
    plus = dict_val.find("+")
    if plus < 0:
        return orig_word
    # letters up to the mark, in the dictionary's own (lowercase) spelling
    stripped = dict_val.replace("+", "")
    core = orig_word.replace(_ACUTE, "")
    if len(core) != len(stripped) or core.lower() != stripped.lower():
        return orig_word            # form mismatch — leave it, let the LLM handle
    vi = plus                        # index of the stressed vowel in `core`
    if vi >= len(core) or core[vi] not in _VOWELS:
        return orig_word
    if core[vi] in "ёЁ":
        return core                 # ё is already unambiguous
    return core[:vi + 1] + _ACUTE + core[vi + 1:]


def _put_mark(word, accented_lower):
    """Place the mark from `accented_lower` (already has a U+0301) onto `word`,
    carrying `word`'s case. Both are the same letters modulo case/mark."""
    plus = accented_lower.find(_ACUTE)
    if plus <= 0:
        return word
    core = word.replace(_ACUTE, "")
    stripped = accented_lower.replace(_ACUTE, "")
    if len(core) != len(stripped) or core.lower() != stripped.lower():
        return word
    vi = plus - 1                       # the stressed vowel's index
    if vi >= len(core) or core[vi] not in _VOWELS or core[vi] in "ёЁ":
        return core
    return core[:vi + 1] + _ACUTE + core[vi + 1:]


def mark_text(text, overrides=None):
    """Dictionary pass. -> (marked_text, uncertain) where `uncertain` is a list
    of {word, reason} for words the dictionary is unsure about (ambiguous
    homograph) or doesn't have (oov). Multi-syllable words only.

    `overrides` = {bare_lower: accented_form} wins over the dictionary — used to
    feed back LLM-resolved homograph / out-of-vocabulary forms on a second call.
    """
    db = _db()
    overrides = overrides or {}
    uncertain = []
    seen = set()

    def repl(m):
        w = m.group(0).replace(_ACUTE, "")
        if _syllables(w) < 2:
            return w
        key = w.lower()
        if key in overrides:
            return _put_mark(w, overrides[key])
        amb = db.execute("SELECT 1 FROM omograph WHERE form=?", (key,)).fetchone()
        row = db.execute("SELECT accented FROM stress WHERE form=?", (key,)).fetchone()
        if amb and key not in seen:
            uncertain.append({"word": w, "reason": "homograph"})
            seen.add(key)
        elif not row and key not in seen:
            uncertain.append({"word": w, "reason": "oov"})
            seen.add(key)
        return _apply(w, row[0]) if row else w

    marked = _CYR_WORD.sub(repl, text)
    return marked, uncertain


# ---------------------------------------------------------------- full pipeline

def strip(text):
    return (text or "").replace(_ACUTE, "")


def to_silero(text):
    """Convert U+0301-after-the-vowel stress marks into Silero TTS's own
    '+'-before-the-vowel format, so the reader is spoken with the dictionary
    stress instead of the model's guess."""
    out = []
    for ch in text or "":
        if ch == _ACUTE and out and out[-1].lower() in "аеиоуыэюяё":
            v = out.pop()
            out.append("+")
            out.append(v)
        elif ch != _ACUTE:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- stress paradigm

def _lookup(form):
    """The dictionary's accented spelling for one wordform, or None."""
    row = _db().execute("SELECT accented FROM stress WHERE form=?",
                        (form.lower().replace("ё", "е"),)).fetchone()
    return _apply(form, row[0]) if row else None


def _stressed_syllable(accented):
    """0-based index of the stressed syllable in a marked word."""
    n = 0
    for i, ch in enumerate(accented):
        if ch in "ёЁ":
            return n
        if ch in _VOWELS:
            if i + 1 < len(accented) and accented[i + 1] == _ACUTE:
                return n
            n += 1
    return -1


_NOUN_SLOTS = [
    ("nom sg", {"nomn", "sing"}), ("gen sg", {"gent", "sing"}),
    ("dat sg", {"datv", "sing"}), ("acc sg", {"accs", "sing"}),
    ("ins sg", {"ablt", "sing"}), ("prep sg", {"loct", "sing"}),
    ("nom pl", {"nomn", "plur"}), ("gen pl", {"gent", "plur"}),
]
_VERB_SLOTS = [
    ("я", {"1per", "sing", "pres"}), ("ты", {"2per", "sing", "pres"}),
    ("он", {"3per", "sing", "pres"}), ("они", {"3per", "plur", "pres"}),
    ("он (past)", {"masc", "sing", "past"}), ("она (past)", {"femn", "sing", "past"}),
    ("они (past)", {"plur", "past"}),
]


_PARADIGM_CACHE = {}


def paradigm(word):
    """Whether a word's stress MOVES as it inflects, and the key forms if so.
    -> {"pattern": "mobile", "forms": [{"label", "form"}]} or None (fixed / n/a).
    Nouns and verbs only."""
    key = strip(word or "").lower().replace("ё", "е")
    if key in _PARADIGM_CACHE:
        return _PARADIGM_CACHE[key]
    r = _paradigm(key)
    if len(_PARADIGM_CACHE) < 20000:
        _PARADIGM_CACHE[key] = r
    return r


def _paradigm(word):
    import db as _rootdb
    try:
        p = _rootdb._morph().parse(strip(word).lower().replace("ё", "е"))[0]
    except Exception:  # noqa: BLE001
        return None
    tag = str(p.tag)
    slots = _NOUN_SLOTS if "NOUN" in tag else (
        _VERB_SLOTS if ("VERB" in tag or "INFN" in tag) else None)
    if not slots:
        return None
    out, idxs = [], set()
    for label, gram in slots:
        try:
            f = p.inflect(gram)
        except Exception:  # noqa: BLE001
            f = None
        if not f or not f.word:
            continue
        if _syllables(f.word) < 2:
            out.append({"label": label, "form": f.word})
            continue
        acc = _lookup(f.word)
        if not acc:
            return None                         # incomplete data — don't half-show
        si = _stressed_syllable(acc)
        if si >= 0:
            idxs.add(si)
        out.append({"label": label, "form": acc})
    if len(idxs) <= 1 or len(out) < 3:
        return None                             # fixed stress (or too little to say)
    return {"pattern": "mobile", "forms": out}


_SENT_SPLIT = re.compile(r"(?<=[.!?…»])\s+")


def _sentence_for(text, bare_word):
    low = bare_word.lower()
    for s in _SENT_SPLIT.split(text):
        if low in s.replace(_ACUTE, "").lower():
            return s.replace(_ACUTE, "").strip()
    return text.replace(_ACUTE, "")[:200]


def accent_paragraphs(paragraphs, passes=1):
    """Plain-text paragraphs -> stress-marked paragraphs.

    The dictionary marks every word (it is reliable for mechanical inflected
    stress). The LLM is then asked ONLY about the words the dictionary itself
    flagged as unresolvable — homographs and out-of-vocabulary words — and its
    answers are substituted for just those words. Nothing else is second-guessed,
    so a dictionary-correct word can't be "corrected" into an error.

    `passes` is accepted for backwards-compat; the resolve step already retries.
    """
    plain = [strip(p) for p in paragraphs]

    all_uncertain = {}          # bare-lower -> (surface, sentence)
    for p in plain:
        _, unc = mark_text(p)
        for u in unc:
            key = u["word"].lower()
            if key not in all_uncertain:
                all_uncertain[key] = (u["word"], _sentence_for(p, u["word"]))

    overrides = {}
    if all_uncertain:
        items = list(all_uncertain.values())
        try:
            resolved = llm.stress_resolve(items)
        except Exception:  # noqa: BLE001
            resolved = [w for w, _ in items]
        for (surface, _), acc in zip(items, resolved):
            acc = (acc or "").strip()
            if acc and _ACUTE in acc and acc.replace(_ACUTE, "").lower() == surface.lower():
                overrides[surface.lower()] = acc

    return [mark_text(p, overrides)[0] for p in plain]


def verify(plain_or_marked):
    """Re-accent from scratch and diff against what's given. -> list of
    {before, after} word-level differences (empty = matches a fresh run).
    Deterministic apart from the small homograph-resolve call."""
    plain = [strip(p) for p in plain_or_marked]
    fresh = accent_paragraphs(plain)
    diffs = []
    for old, new in zip(plain_or_marked, fresh):
        ow = old.split()
        nw = new.split()
        if len(ow) != len(nw):
            continue
        for a, b in zip(ow, nw):
            if a != b:
                diffs.append({"before": a, "after": b})
    return diffs
