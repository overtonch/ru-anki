"""Populate `dict_ru` — a local Russian→English gloss table for the instant
"best effort" translation shown while the LLM call is still in flight. The card
itself is always LLM-translated; this is only a placeholder.

Source: WikDict's Wiktionary-derived ru-en SQLite (~18 MB, ~128k headwords).
  https://download.wikdict.com/dictionaries/sqlite/2/ru-en.sqlite3

Usage:
  python build_dict.py                       # download + build into vocab.db
  python build_dict.py path/to/ru-en.sqlite3 # use a local copy
  python build_dict.py --db other.db
"""
import os
import re
import sqlite3
import sys
import tempfile
import urllib.request

URL = "https://download.wikdict.com/dictionaries/sqlite/2/ru-en.sqlite3"


def norm(w: str) -> str:
    return w.strip().lower().replace("ё", "е")


def _clean_gloss(trans_list: str) -> str:
    parts = [p.strip() for p in (trans_list or "").split("|") if p.strip()]
    seen, out = set(), []
    for p in parts:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
        if len(out) >= 5:
            break
    g = ", ".join(out)
    g = re.sub(r"\s+", " ", g).strip(" ,;")
    if len(g) > 90:
        g = g[:88].rsplit(" ", 1)[0] + "…"
    return g


def main() -> None:
    args = [a for a in sys.argv[1:]]
    db = "vocab.db"
    if "--db" in args:
        i = args.index("--db")
        db = args[i + 1]
        del args[i:i + 2]
    src = args[0] if args else None

    tmp = None
    if not src:
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        print(f"downloading {URL} …")
        with urllib.request.urlopen(
            urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=120,
        ) as r, open(tmp.name, "wb") as f:
            f.write(r.read())
        src = tmp.name
        print(f"  {os.path.getsize(src) // 1_000_000} MB")

    wd = sqlite3.connect(src)
    rows = wd.execute(
        "SELECT written_rep, trans_list, max_score FROM simple_translation "
        "WHERE trans_list IS NOT NULL AND written_rep IS NOT NULL"
    ).fetchall()
    wd.close()

    best: dict[str, tuple[float, str]] = {}
    for written_rep, trans_list, score in rows:
        hw = norm(written_rep)
        if not hw or len(hw) > 80 or not re.search(r"[а-я]", hw):
            continue
        g = _clean_gloss(trans_list)
        if not g:
            continue
        cur = best.get(hw)
        if cur is None or (score or 0) > cur[0]:
            best[hw] = (score or 0.0, g)

    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS dict_ru (headword TEXT PRIMARY KEY, gloss TEXT)")
    con.execute("DELETE FROM dict_ru")
    con.executemany("INSERT INTO dict_ru(headword, gloss) VALUES (?, ?)",
                    [(hw, g) for hw, (_, g) in best.items()])
    con.commit()
    n = con.execute("SELECT count(*) FROM dict_ru").fetchone()[0]
    con.close()
    if tmp:
        os.unlink(tmp.name)
    print(f"dict_ru: {n} headwords")


if __name__ == "__main__":
    main()
