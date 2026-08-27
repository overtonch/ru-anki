"""Subtitle fetch (yt-dlp) + cleaning. No LLM. Mechanical step only."""
import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from subs import extraction_text, new_text_cues  # noqa: E402

YTDLP = os.path.join(ROOT, ".venv", "bin", "yt-dlp")
_LANGS = ("ru", "ru-RU", "ru-orig")


def _run(args):
    return subprocess.run([YTDLP, *args], capture_output=True, text=True)


def _meta_fields(info):
    thumbs = info.get("thumbnails") or []
    thumb = info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None)
    return {
        "channel": info.get("channel") or info.get("uploader"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "thumbnail_url": thumb,
        "duration": info.get("duration"),
    }


def fetch_meta(url):
    """Just the metadata (channel / thumbnail / duration / title). Fast."""
    meta = _run(["-J", "--skip-download", url])
    if meta.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {meta.stderr.strip()[:400]}")
    info = json.loads(meta.stdout)
    return {"title": info.get("title"), **_meta_fields(info)}


def fetch_subs(url):
    """-> dict(title, subs_kind, subs_lang, raw_subs, channel, channel_url,
    thumbnail_url, duration). Raises RuntimeError on failure. Prefers manual
    captions over auto-generated."""
    meta = _run(["-J", "--skip-download", url])
    if meta.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {meta.stderr.strip()[:400]}")
    info = json.loads(meta.stdout)
    title = info.get("title")
    manual = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}

    def pick(d):
        return next((l for l in _LANGS if l in d), None)

    if pick(manual):
        kind, lang = "manual", pick(manual)
    elif pick(autos):
        kind, lang = "auto", pick(autos)
    else:
        raise RuntimeError(
            f"no Russian subtitles for {title!r} "
            f"(manual: {list(manual)[:15]}, auto: {list(autos)[:15]})"
        )

    with tempfile.TemporaryDirectory() as td:
        args = ["--write-subs" if kind == "manual" else "--write-auto-subs",
                "--skip-download", "--sub-langs", lang, "--sub-format", "vtt",
                "-o", os.path.join(td, "%(id)s.%(ext)s"), url]
        dl = _run(args)
        vtts = glob.glob(os.path.join(td, "*.vtt"))
        if not vtts:
            raise RuntimeError(
                f"subtitle download produced no .vtt: {dl.stdout[-300:]} {dl.stderr[-300:]}"
            )
        raw = open(vtts[0], encoding="utf-8").read()

    return {"title": title, "subs_kind": kind, "subs_lang": lang, "raw_subs": raw,
            **_meta_fields(info)}


def subtitle_lines(raw_vtt):
    """-> list of (start_time 'HH:MM:SS', text): de-overlapped cues, one short
    fragment of genuinely-new words each. Backs live search + sentence rebuild."""
    return new_text_cues(raw_vtt)


def transcript_block(raw_vtt):
    """Compact de-overlapped transcript for the extraction prompt."""
    return extraction_text(raw_vtt)
