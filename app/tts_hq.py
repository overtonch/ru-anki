"""Russian TTS for the Speech Lab, conversation partner and flow reading:

  Piper        — local neural TTS (onnxruntime, ~real-time on CPU). Best free
                 quality. espeak-ng phonemisation RESPECTS the U+0301 stress
                 marks the app already carries, so stress is right. Auto-used
                 whenever a `ru_RU-*.onnx` model is present in the piper dir.
  Apple `say`  — the macOS system voice (Milena, or an Enhanced/Siri voice once
                 downloaded in System Settings). Natural, on-device, no bill.
  Silero v4    — local neural TTS via torch; a further fallback. Reads the
                 dictionary stress (U+0301 -> '+' hints).

ElevenLabs (hosted, METERED) is wired up but OFF unless BOTH
`RU_TTS_ALLOW_ELEVENLABS=1` and `ELEVENLABS_API_KEY=…` are set — the key alone
does nothing, so it can't run up a bill.

`synth_to_file(text, out_path[, prefer])` -> (out_path, backend_label). Always
produces a small AAC .m4a via ffmpeg so everything downstream is uniform.
Env: RU_TTS_PIPER_VOICE (default "ru_RU-irina-medium"), RU_TTS_PIPER_LENGTH
(1.0; >1 slower), RU_TTS_PIPER_DIR; RU_TTS_SAY_VOICE (default "Milena"),
RU_TTS_SAY_RATE (words/min).

To add a Piper voice:  cd "$RU_TTS_PIPER_DIR" && python -m piper.download_voices
ru_RU-dmitri-medium   (voices: irina/dmitri/denis/ruslan, all *-medium).
"""
import os
import re
import subprocess
import sys
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

# --- Apple `say` (macOS system voice — natural, on-device, no bill) ---
# Point RU_TTS_SAY_VOICE at an Enhanced / Siri Russian voice once you've
# downloaded one in System Settings › Accessibility › Spoken Content for a big
# quality jump (e.g. "Milena (Enhanced)").
SAY_VOICE = os.environ.get("RU_TTS_SAY_VOICE", "Milena").strip()
SAY_RATE = os.environ.get("RU_TTS_SAY_RATE", "").strip()   # words/min, e.g. "170"

# --- Piper (local neural — best free quality, respects our stress marks) ---
PIPER_DIR = os.environ.get("RU_TTS_PIPER_DIR", "").strip() or os.path.join(
    os.path.expanduser("~/Library/Application Support/ru-anki"), "piper")
PIPER_VOICE = os.environ.get("RU_TTS_PIPER_VOICE", "ru_RU-irina-medium").strip()
PIPER_LENGTH = os.environ.get("RU_TTS_PIPER_LENGTH", "1.0").strip()
_piper = None
_piper_lock = threading.Lock()
_piper_ok_cache = None

# --- Silero (local fallback) ---
SILERO_SPEAKER = os.environ.get("RU_TTS_HQ_SPEAKER", "eugene")
_SR = 48000
_silero = None
_silero_lock = threading.Lock()
_say_voice_ok = None

_SENT = re.compile(r"[^.!?…\n]+[.!?…»\"']*", re.U)


def has_elevenlabs():
    return bool(EL_KEY)


def _piper_model_path():
    """The chosen voice's .onnx if it's there, else any ru_RU-*.onnx in the dir."""
    p = os.path.join(PIPER_DIR, PIPER_VOICE + ".onnx")
    if os.path.exists(p):
        return p
    try:
        for f in sorted(os.listdir(PIPER_DIR)):
            if f.startswith("ru_RU-") and f.endswith(".onnx"):
                return os.path.join(PIPER_DIR, f)
    except OSError:
        pass
    return None


def _piper_ok():
    global _piper_ok_cache
    if os.environ.get("RU_TEST"):
        return False
    if _piper_ok_cache is None:
        try:
            import piper  # noqa: F401
            _piper_ok_cache = bool(_piper_model_path())
        except Exception:  # noqa: BLE001
            _piper_ok_cache = False
    return bool(_piper_ok_cache)


def _load_piper():
    global _piper
    if _piper is None:
        with _piper_lock:
            if _piper is None:
                from piper import PiperVoice
                mp = _piper_model_path()
                _piper = (PiperVoice.load(mp), os.path.basename(mp)[:-5])
    return _piper


def _silero_ok():
    if os.environ.get("RU_TEST"):
        return False
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def _say_ok():
    """macOS `say` present with a usable Russian voice."""
    global _say_voice_ok
    if os.environ.get("RU_TEST") or not sys.platform.startswith("darwin"):
        return False
    if _say_voice_ok is None:
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True,
                                 text=True, timeout=10).stdout
            _say_voice_ok = SAY_VOICE.lower() in out.lower() or "ru_ru" in out.lower()
        except Exception:  # noqa: BLE001
            _say_voice_ok = False
    return bool(_say_voice_ok)


def _say_voice():
    """The configured voice if it exists, else any installed ru_RU voice."""
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return SAY_VOICE
    for ln in out.splitlines():
        if ln.strip().lower().startswith(SAY_VOICE.lower()):
            return SAY_VOICE
    for ln in out.splitlines():
        if "ru_RU" in ln:
            return ln.split()[0]
    return SAY_VOICE


def backend(prefer=None):
    """The concrete backend — 'elevenlabs' | 'piper' | 'apple' | 'silero' |
    'none'. prefer: force one; None auto-picks the best available (Piper if a
    model is present, then Apple `say`, then Silero). ElevenLabs only when
    explicitly keyed + allowed."""
    if os.environ.get("RU_TEST"):        # never touch a real API from the test suite
        return "none"
    if prefer == "elevenlabs":
        return "elevenlabs" if EL_KEY else backend(None)
    if prefer == "piper":
        return "piper" if _piper_ok() else backend(None)
    if prefer == "apple":
        return "apple" if _say_ok() else backend(None)
    if prefer == "silero":
        return "silero" if _silero_ok() else backend(None)
    if EL_KEY:
        return "elevenlabs"
    if _piper_ok():
        return "piper"
    if _say_ok():
        return "apple"
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


def _to_plus_stress(text):
    """U+0301-after-the-vowel  ->  Silero's '+'-before-the-vowel stress format."""
    out = []
    for ch in text or "":
        if ch == "́" and out and out[-1].lower() in "аеиоуыэюяё":
            v = out.pop()
            out.append("+")
            out.append(v)
        elif ch not in ("́", "̀"):
            out.append(ch)
    return "".join(out)


def _silero_synth(text, out_path):
    import numpy as np
    import scipy.io.wavfile as wav

    text = _to_plus_stress(text)          # speak the dictionary stress, not the guess
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


# ---------------------------------------------------------------- Piper

def _piper_synth(text, out_path):
    import wave

    from piper import SynthesisConfig
    voice, label = _load_piper()
    # keep the U+0301 marks — espeak-ng phonemisation reads them for stress —
    # but drop the grave (unused) and normalise whitespace
    clean = re.sub(r"[ \t]+", " ", (text or "").replace("̀", "")).strip()
    if not re.search(r"[а-яёА-ЯЁ]", clean):
        raise RuntimeError("nothing to speak")
    try:
        length = float(PIPER_LENGTH)
    except ValueError:
        length = 1.0
    cfg = SynthesisConfig(length_scale=length, normalize_audio=True)
    src = out_path + ".src.wav"
    with wave.open(src, "wb") as w:
        voice.synthesize_wav(clean, w, syn_config=cfg)
    if not os.path.exists(src) or os.path.getsize(src) < 200:
        raise RuntimeError("piper produced no audio")
    _to_m4a(src, out_path)
    return "piper:" + label


# ---------------------------------------------------------------- Apple `say`

def _say_synth(text, out_path):
    voice = _say_voice()
    aiff = out_path + ".src.aiff"
    # `say` wants plain text — its Russian voice has its own prosody; combining
    # stress marks confuse it, so strip them
    clean = (text or "").replace("́", "").replace("̀", "").strip()
    cmd = ["say", "-v", voice, "-o", aiff, "--file-format=AIFF"]
    if SAY_RATE.isdigit():
        cmd += ["-r", SAY_RATE]
    cmd += [clean]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(aiff):
        raise RuntimeError(f"say failed: {(r.stderr or '')[-200:]}")
    _to_m4a(aiff, out_path)
    return "apple:" + voice


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
    """-> (out_path, backend_label). prefer: 'elevenlabs' | 'apple' | 'silero' |
    None (auto). Raises RuntimeError on failure. Apple `say` and Silero both fall
    back to each other on error; nothing here touches paid credits."""
    b = backend(prefer)
    if b == "elevenlabs":
        try:
            return out_path, _elevenlabs(text, out_path)
        except Exception as e:  # noqa: BLE001
            print(f"[tts_hq] ElevenLabs failed ({e}) — falling back", flush=True)
            b = "piper" if _piper_ok() else ("apple" if _say_ok() else "silero")
    if b == "piper":
        try:
            return out_path, _piper_synth(text, out_path)
        except Exception as e:  # noqa: BLE001
            print(f"[tts_hq] piper failed ({e}) — trying `say`", flush=True)
            b = "apple" if _say_ok() else "silero"
    if b == "apple":
        try:
            return out_path, _say_synth(text, out_path)
        except Exception as e:  # noqa: BLE001
            print(f"[tts_hq] say failed ({e}) — trying Silero", flush=True)
            if _silero_ok():
                return out_path, _silero_synth(text, out_path)
            raise
    if b == "silero":
        return out_path, _silero_synth(text, out_path)
    raise RuntimeError("no TTS backend available")
