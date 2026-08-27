"""Fetch Russian subtitles for a YouTube video and store the raw result in the DB.

Prefers manual captions over auto-generated. Stores the raw .vtt text against the
video row; extraction (done by Claude, not here) reads that text back out.

Usage: python fetch_subs.py <youtube_url> [db_path]
"""
import glob
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
YTDLP = os.path.join(HERE, ".venv", "bin", "yt-dlp")
DB = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "vocab.db")


def run(args):
    return subprocess.run([YTDLP, *args], capture_output=True, text=True)


def main() -> None:
    url = sys.argv[1]

    meta = run(["-J", "--skip-download", url])
    if meta.returncode != 0:
        sys.exit(f"yt-dlp metadata failed:\n{meta.stderr}")
    info = json.loads(meta.stdout)
    title = info.get("title")
    manual = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}

    def pick(d):
        for lang in ("ru", "ru-RU", "ru-orig"):
            if lang in d:
                return lang
        return None

    kind, lang = None, None
    if pick(manual):
        kind, lang = "manual", pick(manual)
    elif pick(autos):
        kind, lang = "auto", pick(autos)
    else:
        sys.exit(f"No Russian subtitles (manual or auto) for: {title}\n"
                 f"manual langs: {list(manual)[:20]}\nauto langs: {list(autos)[:20]}")

    with tempfile.TemporaryDirectory() as td:
        args = ["--skip-download", "--sub-langs", lang, "--sub-format", "vtt",
                "-o", os.path.join(td, "%(id)s.%(ext)s"), url]
        args.insert(0, "--write-subs" if kind == "manual" else "--write-auto-subs")
        dl = run(args)
        vtts = glob.glob(os.path.join(td, "*.vtt"))
        if not vtts:
            sys.exit(f"subtitle download produced no .vtt:\n{dl.stdout}\n{dl.stderr}")
        raw = open(vtts[0], encoding="utf-8").read()

    con = sqlite3.connect(DB)
    con.execute(
        """INSERT INTO videos(url, title, subs_kind, subs_lang, raw_subs)
           VALUES(?,?,?,?,?)
           ON CONFLICT(url) DO UPDATE SET
             title=excluded.title, subs_kind=excluded.subs_kind,
             subs_lang=excluded.subs_lang, raw_subs=excluded.raw_subs,
             fetched_at=datetime('now')""",
        (url, title, kind, lang, raw),
    )
    con.commit()
    vid = con.execute("SELECT id FROM videos WHERE url=?", (url,)).fetchone()[0]
    con.close()

    print(f"video_id={vid}")
    print(f"title={title}")
    print(f"subs={kind}/{lang}  chars={len(raw)}")


if __name__ == "__main__":
    main()
