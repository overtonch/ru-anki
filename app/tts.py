"""Local Russian text-to-speech via the built-in macOS `say` command.

Free, offline, no API key — used to give a "listen" clip to cards whose source
is text (a book, an article, a pasted passage), which have no recorded audio.
Output is a small AAC .m4a, cached on disk by a hash of (voice, rate, text)."""
import hashlib
import os
import subprocess

import ytdlp  # for MEDIA_DIR — TTS clips live alongside the video clips

VOICE = os.environ.get("RU_TTS_VOICE", "Milena")     # the macOS ru_RU voice
RATE = os.environ.get("RU_TTS_RATE", "180")          # words per minute
TTS_DIR = os.path.join(ytdlp.MEDIA_DIR, "tts")

_available = None


def available():
    """True if `say` has the configured Russian voice. Cached."""
    global _available
    if _available is None:
        try:
            r = subprocess.run(["say", "-v", "?"], capture_output=True,
                               text=True, timeout=10)
            _available = VOICE.lower() in r.stdout.lower()
        except Exception:  # noqa: BLE001
            _available = False
    return _available


def _key(text):
    return hashlib.sha1(f"{VOICE}|{RATE}|{text}".encode()).hexdigest()[:16]


def path_for(text):
    return os.path.join(TTS_DIR, _key(_clean(text)) + ".m4a")


def _clean(text):
    return " ".join((text or "").split())[:400]


def synthesize(text):
    """-> path to an AAC .m4a of `text` read aloud in Russian. Cached; ~0.5 s
    to generate a sentence. Raises RuntimeError."""
    text = _clean(text)
    if not text:
        raise RuntimeError("nothing to speak")
    out = path_for(text)
    if os.path.exists(out) and os.path.getsize(out) > 400:
        return out
    if not available():
        raise RuntimeError(f"macOS voice {VOICE!r} not installed")
    os.makedirs(TTS_DIR, exist_ok=True)
    tmp = out + ".part"
    r = subprocess.run(
        ["say", "-v", VOICE, "-r", str(RATE),
         "--file-format=m4af", "--data-format=aac", "-o", tmp, text],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError(f"say failed: {(r.stderr or '')[-200:]}")
    os.replace(tmp, out)
    return out
