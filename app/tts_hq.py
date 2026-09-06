"""Russian TTS for the Speech Lab + conversation partner:

  Silero v4   — local, offline, no key, no bill. The default and only backend.

ElevenLabs (hosted, more natural, but METERED) is wired up but OFF by default so
it can never run up a bill. To re-enable it, set both
`RU_TTS_ALLOW_ELEVENLABS=1` and `ELEVENLABS_API_KEY=…` in
`~/Library/Application Support/ru-anki/secrets.env`. Without the allow flag the
key is ignored entirely and every call uses Silero.

`synth_to_file(text, out_path)` returns (out_path, backend_label). It always
produces a small AAC .m4a via ffmpeg so everything downstream is uniform.
"""
import os
import re
import subprocess
import threading

import ytdlp  # for MEDIA_DIR

HQ_DIR = os.path.join(ytdlp.MEDIA_DIR, "speech-audio")

# --- optional secrets file (so a launchd restart keeps the key) ---
_SECRETS = os.path.join(
    os.path.expanduser("~/Library/Application Support/ru-anki"), "secrets.env")
if os.path.exists(_SECRETS):
    try:
        for _line in open(_SECRETS, encoding="utf-8"):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except OSError:
        pass

# --- ElevenLabs (metered — OFF unless explicitly allowed, so it can't bill) ---
_ALLOW_EL = os.environ.get("RU_TTS_ALLOW_ELEVENLABS", "").strip().lower() in (
    "1", "true", "yes", "on")
EL_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip() if _ALLOW_EL else ""
EL_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB").strip()  # "Adam"
EL_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2").strip()
EL_URL = "https://api.elevenlabs.io/v1/text-to-speech/{vid}"

# --- Silero (local fallback) ---
SILERO_SPEAKER = os.environ.get("RU_TTS_HQ_SPEAKER", "eugene")
_SR = 48000
_silero = None
_silero_lock = threading.Lock()

_SENT = re.compile(r"[^.!?…\n]+[.!?…»\"']*", re.U)


def has_elevenlabs():
    return bool(EL_KEY)


def _silero_ok():
    if os.environ.get("RU_TEST"):
        return False
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def backend(prefer=None):
    """The concrete backend for a given preference — 'elevenlabs', 'silero', or
    'none'. prefer: 'elevenlabs' | 'silero' | None (auto: ElevenLabs if keyed)."""
    if os.environ.get("RU_TEST"):        # never touch a real API from the test suite
        return "none"
    if prefer == "elevenlabs" and EL_KEY:
        return "elevenlabs"
    if prefer == "silero":
        return "silero" if _silero_ok() else "none"
    if EL_KEY:
        return "elevenlabs"
    # ElevenLabs unavailable (disabled or unkeyed) — always fall back to local
    return "silero" if _silero_ok() else "none"


def available(prefer=None):
    return backend(prefer) != "none"


def split_sentences(text):
    """Sentences in reading order; a bare None marks a paragraph break."""
    out = []
    for para in (text or "").replace("\r", "").split("\n"):
        para = para.strip()
        if not para:
            if out and out[-1] is not None:
                out.append(None)
            continue
        for m in _SENT.finditer(para):
            s = m.group(0).strip()
            if s and re.search(r"[а-яёА-ЯЁ]", s):
                out.append(s)
    while out and out[-1] is None:
        out.pop()
    return out


# ---------------------------------------------------------------- ElevenLabs

def _elevenlabs(text, out_path):
    import httpx

    body = {
        "text": text,
        "model_id": EL_MODEL,
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.85,
                           "style": 0.0, "use_speaker_boost": True},
    }
    with httpx.Client(timeout=180) as cli:
        r = cli.post(EL_URL.format(vid=EL_VOICE),
                     params={"output_format": "mp3_44100_128"},
                     headers={"xi-api-key": EL_KEY, "accept": "audio/mpeg"},
                     json=body)
    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("detail", {})
            detail = detail.get("message") or str(detail)
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        raise RuntimeError(f"ElevenLabs {r.status_code}: {detail}")
    mp3 = out_path + ".src.mp3"
    with open(mp3, "wb") as f:
        f.write(r.content)
    _to_m4a(mp3, out_path)
    return "elevenlabs:" + EL_VOICE


# ---------------------------------------------------------------- Silero

def _load_silero():
    global _silero
    with _silero_lock:
        if _silero is None:
            import torch
            torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))
            m, _ = torch.hub.load("snakers4/silero-models", "silero_tts",
                                  language="ru", speaker="v4_ru", trust_repo=True)
            m.to("cpu")
            _silero = m
    return _silero


def _silero_synth(text, out_path):
    import numpy as np
    import scipy.io.wavfile as wav

    m = _load_silero()
    gap = np.zeros(int(_SR * 0.35), dtype=np.float32)
    para_gap = np.zeros(int(_SR * 0.75), dtype=np.float32)
    parts = []
    for s in split_sentences(text):
        if s is None:
            if parts:
                parts[-1] = para_gap
            continue
        try:
            au = m.apply_tts(text=s, speaker=SILERO_SPEAKER, sample_rate=_SR,
                             put_accent=True, put_yo=True)
        except Exception as e:  # noqa: BLE001
            print(f"[tts_hq] silero sentence failed ({e}): {s[:60]!r}", flush=True)
            continue
        parts.append(np.asarray(au, dtype=np.float32))
        parts.append(gap)
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        raise RuntimeError("nothing synthesised")
    full = np.concatenate(parts)
    peak = float(np.abs(full).max()) or 1.0
    full = (full / peak * 0.95 * 32767).astype(np.int16)
    src = out_path + ".src.wav"
    wav.write(src, _SR, full)
    _to_m4a(src, out_path)
    return "silero:" + SILERO_SPEAKER


# ---------------------------------------------------------------- shared

def _to_m4a(src, out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ac", "1", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", out_path],
        capture_output=True, text=True, timeout=300)
    try:
        os.remove(src)
    except OSError:
        pass
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg failed: {(r.stderr or '')[-200:]}")


def synth_to_file(text, out_path, prefer=None):
    """-> (out_path, backend_label). prefer: 'elevenlabs' | 'silero' | None.
    Raises RuntimeError on failure. An explicit 'silero' never touches credits;
    'elevenlabs' falls back to Silero only on a transient API error."""
    b = backend(prefer)
    if b == "elevenlabs":
        try:
            return out_path, _elevenlabs(text, out_path)
        except Exception as e:  # noqa: BLE001
            print(f"[tts_hq] ElevenLabs failed ({e}) — trying Silero", flush=True)
            if _silero_ok():
                return out_path, _silero_synth(text, out_path)
            raise
    if b == "silero":
        return out_path, _silero_synth(text, out_path)
    raise RuntimeError("no TTS backend available (local Silero needs torch)")
