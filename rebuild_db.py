"""Rebuild vocab.db from the plain-text git backup (videos/candidates/
resolved_words .ndjson). Use this if the working DB and the iCloud snapshots
are both gone.

Usage:
  python rebuild_db.py <path-to-backup-git-dir> [out.db]

Then re-derive subtitle_lines and the stoplist:
  python -c "import sys;sys.path.insert(0,'app');import store,ytdlp; \
             [store.replace_subtitle_lines(v['id'], ytdlp.subtitle_lines(store.get_video(v['id'])['raw_subs'])) \
              for v in store.list_videos()]"
  python build_stoplist.py vocab.db
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def insert(c, table, records):
    n = 0
    for r in records:
        cols = list(r)
        c.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                  f"VALUES ({','.join('?' for _ in cols)})", [r[k] for k in cols])
        n += 1
    return n


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "vocab.db")
    if os.path.exists(out):
        sys.exit(f"{out} already exists — move it aside first")

    c = sqlite3.connect(out)
    for fn in ("schema.sql", "schema_v2.sql"):
        c.executescript(open(os.path.join(HERE, fn), encoding="utf-8").read())
    # schema_v2 adds columns via ALTER in code, not SQL — add them here too
    for col in ("source TEXT", "channel TEXT", "channel_url TEXT",
                "thumbnail_url TEXT", "duration INTEGER"):
        try:
            tbl = "candidates" if col.startswith("source") else "videos"
            c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    v = insert(c, "videos", rows(os.path.join(src, "videos.ndjson")))
    k = insert(c, "candidates", rows(os.path.join(src, "candidates.ndjson")))
    r = insert(c, "resolved_words", rows(os.path.join(src, "resolved_words.ndjson")))
    c.commit()
    c.close()
    print(f"rebuilt {out}: {v} videos, {k} candidates, {r} resolved_words")
    print("now re-derive subtitle_lines + stoplist (see this file's docstring)")


if __name__ == "__main__":
    main()
