"""Restore vocab.db from a backup snapshot.

Stop the server first (the working DB must not be open). By default restores
`vocab-latest.db` from the backup dir; pass a path to pick a specific snapshot.

Usage:
  python restore_backup.py                       # newest (vocab-latest.db)
  python restore_backup.py --list                # show available snapshots
  python restore_backup.py <path-to-snapshot.db> # a specific one
"""
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("VOCAB_DB", os.path.join(HERE, "vocab.db"))
BACKUP_DIR = os.environ.get(
    "RU_BACKUP_DIR",
    os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/ru-anki-backup"),
)


def snapshots():
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted(f for f in os.listdir(BACKUP_DIR)
                  if f.startswith("vocab-") and f.endswith(".db"))


def main():
    args = sys.argv[1:]
    if args and args[0] == "--list":
        for f in snapshots():
            p = os.path.join(BACKUP_DIR, f)
            print(f"{f}\t{os.path.getsize(p):>10} bytes\t{os.path.getmtime(p)}")
        return

    src = args[0] if args else os.path.join(BACKUP_DIR, "vocab-latest.db")
    if not os.path.exists(src):
        sys.exit(f"no snapshot at {src}\nrun with --list to see what's available")

    # sanity-check it opens and has our tables
    c = sqlite3.connect(src)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    n = c.execute("SELECT count(*) FROM candidates").fetchone()[0]
    c.close()
    missing = {"videos", "candidates", "resolved_words"} - tables
    if missing:
        sys.exit(f"{src} is missing tables: {missing}")

    if os.path.exists(DB):
        bak = DB + ".pre-restore"
        shutil.copy2(DB, bak)
        print(f"current DB backed up to {bak}")
        for ext in ("-wal", "-shm"):
            if os.path.exists(DB + ext):
                os.remove(DB + ext)
    shutil.copy2(src, DB)
    print(f"restored {src} -> {DB}  ({n} candidates)")
    print("restart the server: ./run.sh")


if __name__ == "__main__":
    main()
