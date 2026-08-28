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
_LANGS = ("ru", "ru-RU", "ru-orig", "rus")


def _pick_lang(d):
    """A Russian subtitle track key from a yt-dlp {lang: [...]} dict — exact
    match first, then anything starting 'ru' (ru-RU, ru_RU, ru-x-…, russian)."""
    for want in _LANGS:
        if want in d:
            return want
    for k in d:
        kl = k.lower().replace("_", "-")
        if kl == "ru" or kl.startswith("ru-") or kl.startswith("rus"):
            return k
    return None


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

    mlang, alang = _pick_lang(manual), _pick_lang(autos)
    if mlang:
        kind, lang = "manual", mlang
    elif alang:
        kind, lang = "auto", alang
    else:
        raise RuntimeError(
            f"no Russian subtitles for {title!r} "
            f"(manual: {list(manual)[:15]}, auto: {list(autos)[:15]})"
        )

    with tempfile.TemporaryDirectory() as td:
        args = ["--write-subs" if kind == "manual" else "--write-auto-subs",
                "--skip-download", "--sub-langs", lang,
                "--sub-format", "vtt/srt/best", "--convert-subs", "vtt",
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


MEDIA_DIR = os.environ.get(
    "RU_MEDIA_DIR",
    os.path.expanduser("~/Library/Application Support/ru-anki/media"))


def download_media(url, video_id, height=360, progress=None):
    """Download a Safari-compatible MP4 (H.264 video + AAC audio) for offline
    watching. -> (path, bytes). Raises RuntimeError on failure.

    `progress(pct_float)` is called with yt-dlp's download percentage.
    """
    os.makedirs(MEDIA_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(MEDIA_DIR, f"{video_id}.*")):
        try:
            os.remove(old)
        except OSError:
            pass
    fmt = (f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]/"
           f"b[height<={height}][ext=mp4]/b[ext=mp4]/b[height<={height}]/b")
    out = os.path.join(MEDIA_DIR, f"{video_id}.%(ext)s")
    proc = subprocess.Popen(
        # -S keeps the pick near `height` even when a source (VK/HLS) doesn't
        # expose a format that satisfies the [height<=] filters
        [YTDLP, "-f", fmt, "-S", f"res:{height},codec:h264,+size,+br",
         "--merge-output-format", "mp4", "--no-playlist",
         "--newline", "--no-part", "-o", out, url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    for line in proc.stdout:
        tail.append(line)
        if progress and "[download]" in line and "%" in line:
            try:
                progress(float(line.split("%")[0].split()[-1]))
            except (ValueError, IndexError):
                pass
    if proc.wait() != 0:
        raise RuntimeError("yt-dlp download failed: " + "".join(tail[-6:])[:500])
    files = [f for f in glob.glob(os.path.join(MEDIA_DIR, f"{video_id}.*"))
             if not f.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("download produced no file")
    path = files[0]
    return path, os.path.getsize(path)


def audio_path(video_id):
    """Path to the downloaded audio-only file for this video, or None."""
    hits = [f for f in glob.glob(os.path.join(MEDIA_DIR, f"{video_id}.audio.*"))
            if not f.endswith((".part", ".ytdl"))]
    return hits[0] if hits else None


def download_audio(url, video_id, progress=None):
    """Download an audio-only m4a (AAC) for background / screen-off listening.
    -> (path, bytes). Much smaller and faster than the video. `progress(pct)`."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(MEDIA_DIR, f"{video_id}.audio.*")):
        try:
            os.remove(old)
        except OSError:
            pass
    fmt = "ba[ext=m4a]/ba[acodec^=mp4a]/ba/b"
    out = os.path.join(MEDIA_DIR, f"{video_id}.audio.%(ext)s")
    proc = subprocess.Popen(
        [YTDLP, "-f", fmt, "-x", "--audio-format", "m4a", "--no-playlist",
         "--newline", "--no-part", "-o", out, url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    for line in proc.stdout:
        tail.append(line)
        if progress and "[download]" in line and "%" in line:
            try:
                progress(float(line.split("%")[0].split()[-1]))
            except (ValueError, IndexError):
                pass
    if proc.wait() != 0:
        raise RuntimeError("yt-dlp audio download failed: " + "".join(tail[-6:])[:500])
    path = audio_path(video_id)
    if not path:
        raise RuntimeError("audio download produced no file")
    return path, os.path.getsize(path)


def resolve_stream(url, height=360):
    """Resolve a single progressive (muxed video+audio, non-HLS) http MP4 URL
    plus the request headers yt-dlp would use, for proxy-streaming to the phone.
    -> (media_url, headers). Raises RuntimeError."""
    r = _run(["-J", "--skip-download", url])
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp resolve failed: {r.stderr.strip()[:300]}")
    info = json.loads(r.stdout)
    fmts = info.get("formats") or []

    def http_mp4(f):
        return (str(f.get("protocol", "")).startswith("http")
                and (f.get("ext") == "mp4" or "mp4" in str(f.get("format_id", "")))
                and f.get("url"))

    # VK/others expose legacy muxed formats as url144/url240/... — prefer those
    muxed = [f for f in fmts if http_mp4(f)
             and (str(f.get("format_id", "")).startswith("url")
                  or (f.get("vcodec", "none") != "none"
                      and f.get("acodec", "none") not in ("none", None)))]
    pool = muxed or [f for f in fmts if http_mp4(f) and f.get("vcodec", "none") != "none"]
    if not pool:
        raise RuntimeError("no progressive MP4 stream (only HLS/DASH?)")
    pool.sort(key=lambda f: ((f.get("height") or 0) > height,
                             abs((f.get("height") or 9999) - height)))
    f = pool[0]
    return f["url"], (f.get("http_headers") or {})


def subtitle_lines(raw_vtt):
    """-> list of (start_time 'HH:MM:SS', text): de-overlapped cues, one short
    fragment of genuinely-new words each. Backs live search + sentence rebuild."""
    return new_text_cues(raw_vtt)


def transcript_block(raw_vtt):
    """Compact de-overlapped transcript for the extraction prompt."""
    return extraction_text(raw_vtt)
