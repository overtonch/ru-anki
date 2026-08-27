"""Populate the stoplist table from a public Russian *lemma* frequency list.

Source: hingston/russian, 50000-russian-words-cyrillic-only.txt - lemmas
(infinitives, nominative singular, ...) ordered by frequency. This is a lemma
list, not a surface-form list, so a rank means what it should. We take the top
CUTOFF lemmas as the "don't bother asking me about this" backstop; candidates
are lemmatised with pymorphy3 before being checked against it (see db.py).

CUTOFF is calibrated to the user (2026-08): they knew every probe word at
lemma-rank <= ~11.5k and wanted a card for every probe at rank >= ~15.5k, so
13k sits in the empty gap between.
"""
import sqlite3
import sys
import urllib.request

URL = ("https://raw.githubusercontent.com/hingston/russian/master/"
       "50000-russian-words-cyrillic-only.txt")
CUTOFF = 13000
DB = sys.argv[1] if len(sys.argv) > 1 else "vocab.db"


def norm(w: str) -> str:
    return w.strip().lower().replace("ё", "е")


def main() -> None:
    data = urllib.request.urlopen(
        urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"}), timeout=30
    ).read().decode("utf-8", "replace")

    rows, seen = [], set()
    for line in data.splitlines():
        w = norm(line)
        if not w or w in seen or not all("а" <= c <= "я" or c == "-" for c in w):
            continue
        seen.add(w)
        rows.append((w, len(rows) + 1))
        if len(rows) >= CUTOFF:
            break

    con = sqlite3.connect(DB)
    con.execute("DELETE FROM stoplist")
    con.executemany("INSERT INTO stoplist(normalized_text, rank) VALUES (?, ?)", rows)
    con.commit()
    n = con.execute("SELECT count(*) FROM stoplist").fetchone()[0]
    print(f"stoplist: {n} lemmas (cutoff {CUTOFF})")
    con.close()


if __name__ == "__main__":
    main()
