"""Build a book's lemma-frequency profile for the reading-readiness metric.

    python build_book_vocab.py anna_karenina path/to/text.txt "Анна Каренина" "Л. Н. Толстой" <source-url>

Writes app/data/books/<id>.freq.json.gz = {meta, freq:{lemma: count}}.
Proper nouns (names, patronymics, places, orgs) and non-Cyrillic junk are
dropped — the metric is about vocabulary, not who's who.
"""
import collections
import gzip
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

_SKIP_TAGS = ("Name", "Surn", "Patr", "Geox", "Orgn", "Trad", "Abbr")
_WORD = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё-]+")
# hyphenated grammar words a B2 reader already has — не vocabulary to target
_KNOWN_HYPHEN = {
    "что-то", "что-нибудь", "кто-то", "кто-нибудь", "какой-то", "какой-нибудь",
    "чей-то", "где-то", "где-нибудь", "куда-то", "куда-нибудь", "когда-то",
    "когда-нибудь", "как-то", "как-нибудь", "сколько-нибудь", "почему-то",
    "зачем-то", "все-таки", "всё-таки", "из-за", "из-под", "по-моему",
    "по-твоему", "по-нашему", "по-русски", "по-французски", "по-английски",
    "по-немецки", "по-прежнему", "по-настоящему", "по-видимому", "во-первых",
    "во-вторых", "в-третьих", "потихоньку", "мало-помалу",
}


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    book_id, path = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else book_id
    author = sys.argv[4] if len(sys.argv) > 4 else ""
    source = sys.argv[5] if len(sys.argv) > 5 else ""

    text = open(path, encoding="utf-8").read()
    raw = [w for w in _WORD.findall(text) if len(w) >= 2]
    morph = db._morph()
    lemma_cache, skip_cache = {}, {}
    counts = collections.Counter()
    caps = collections.Counter()          # capitalised occurrences per lemma
    for w in raw:
        lw = w.lower()
        lem = lemma_cache.get(lw)
        if lem is None:
            p = morph.parse(lw.replace("ё", "е"))[0]
            lem = db.norm(p.normal_form)
            lemma_cache[lw] = lem
            skip_cache[lem] = skip_cache.get(lem) or any(
                t in str(p.tag) for t in _SKIP_TAGS)
        if skip_cache.get(lem):
            continue
        counts[lem] += 1
        if w[:1].isupper():
            caps[lem] += 1
    # a lemma capitalised in >70% of its occurrences (and seen a few times) is a
    # proper noun pymorphy's tags missed — drop it
    for lem, n in list(counts.items()):
        if n >= 4 and caps[lem] / n > 0.70:
            del counts[lem]

    # second pass against the app's own lexicons: fold a noisy lemma into a
    # cleaner form when pymorphy under-lemmatised, and drop what's clearly not a
    # Russian dictionary word (leftover names, OCR debris)
    dbf = os.environ.get("VOCAB_DB", "vocab.db")
    real = set()
    if os.path.exists(dbf):
        con = sqlite3.connect(dbf)
        lemset = list(counts)
        ph = ",".join("?" * len(lemset))
        real |= {r[0] for r in con.execute(
            f"SELECT normalized_text FROM freq WHERE normalized_text IN ({ph})", lemset)}
        real |= {r[0] for r in con.execute(
            f"SELECT headword FROM dict_ru WHERE headword IN ({ph})", lemset)}
        real |= {r[0] for r in con.execute(
            f"SELECT normalized_text FROM stoplist WHERE normalized_text IN ({ph})", lemset)}
        con.close()
    merged = collections.Counter()
    for lem, n in counts.items():
        if lem in _KNOWN_HYPHEN:
            continue
        if lem in real or "-" in lem:
            merged[lem] += n
            continue
        alt = db.lemma_key(lem)
        merged[(alt if alt and alt in real else lem)] += n
    counts = merged
    kept_tokens = sum(counts.values())

    out = {
        "meta": {"title": title, "author": author, "source": source,
                 "tokens": kept_tokens, "raw_tokens": len(raw),
                 "types": len(counts)},
        "freq": dict(counts.most_common()),
    }
    dst = os.path.join("app", "data", "books", f"{book_id}.freq.json.gz")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with gzip.open(dst, "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"{dst}: {kept_tokens} tokens ({len(raw)} raw), {len(counts)} lemmas, "
          f"{os.path.getsize(dst)} bytes")
    print("top 25:", list(counts.most_common(25)))


if __name__ == "__main__":
    main()
