"""Rebuild vocab.db from the plain-text git backup (RU_BACKUP_GIT_DIR).

Use this if the working DB *and* the local snapshots are both gone (e.g. the Mac
died). The git repo holds every table you can't cheaply regenerate; this script
recreates the schema, loads them, re-derives subtitle_lines from videos.raw_subs,
and leaves only the lookup tables (freq / stoplist / dict_ru) for the build
scripts.

Usage:
  python rebuild_db.py <path-to-backup-git-dir> [out.db]

Afterwards, rebuild the lookup tables:
  python build_stoplist.py <out.db>
  python build_dict.py --db <out.db>
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# load order: parents before children so FK checks stay happy
TABLES = ("videos", "candidates", "resolved_words", "texts", "text_chapters",
          "srs_cards", "srs_reviews", "app_settings",
          "word_accent", "word_family", "word_gloss", "lyric_notes",
          "drill_lapse", "drill_items", "motion_lapse", "motion_items",
          "chunk_stage", "chunk_lapse", "chunk_items", "journal_sessions", "speeches",
          "reading_flow_sessions", "reading_flow_chunks", "reading_flow_unknown",
          "convo_sessions", "convo_turns", "proficiency_snapshots",
          "activate_items", "activate_log")


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

    # build the current schema (schema.sql + schema_v2.sql + every ALTER) by
    # pointing the app's own init_db at the new file
    os.environ["VOCAB_DB"] = out
    sys.path.insert(0, os.path.join(HERE, "app"))
    import store  # noqa: E402
    store.init_db()

    c = sqlite3.connect(out)
    c.execute("PRAGMA foreign_keys=OFF")
    loaded = {}
    for t in TABLES:
        loaded[t] = insert(c, t, rows(os.path.join(src, f"{t}.ndjson")))
    c.commit()
    c.close()

    # subtitle_lines are fully derivable from videos.raw_subs
    import ytdlp  # noqa: E402
    n_lines = 0
    for v in store.list_videos(include_hidden=True):
        raw = store.raw_subs(v["id"])
        if raw:
            n_lines += store.replace_subtitle_lines(
                v["id"], ytdlp.subtitle_lines(raw))

    print(f"rebuilt {out}:")
    for t in TABLES:
        print(f"  {t:<16} {loaded[t]}")
    print(f"  subtitle_lines   {n_lines} (re-derived)")
    print("\nnow rebuild the lookup tables:")
    print(f"  python build_stoplist.py {out}")
    print(f"  python build_dict.py --db {out}")


if __name__ == "__main__":
    main()
