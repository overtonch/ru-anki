"""Local Whisper re-transcription (no API cost — pure compute). Replaces sketchy
auto-captions or fills in content that has none.

Default backend: **mlx-whisper** (large-v3-turbo on the Apple GPU) with pipelined
ffmpeg extraction, VAD gating, and an optional parallel faster-whisper CPU worker
(RU_WHISPER_CPU_WORKER=1). On a base M1 this measured ~8-9x realtime.

Alternate backend: **whisper.cpp + CoreML** (RU_WHISPER_BACKEND=whispercpp). The
encoder runs through CoreML (meant for the ANE) and one process handles the whole
file. Built + measured 2026-08-28 (see deploy/WHISPERCPP.md) — it came out ~6x
realtime, i.e. *slower* than MLX turbo on this M1, so it's opt-in, not the
default. Kept wired up in case a machine with a bigger ANE / a longer file
changes the balance.

`condition_on_previous_text` stays on within each window / ~10-min chunk.
"""
import json
import math
import os
import queue
import re
import subprocess
import threading

import ytdlp

MODEL = os.environ.get("RU_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
_CHUNK = int(os.environ.get("RU_WHISPER_CHUNK", "600"))        # seconds per pass

# ---- whisper.cpp (CoreML/ANE) backend ----
_WCPP_DIR = os.environ.get(
    "RU_WHISPERCPP_DIR",
    os.path.expanduser("~/Library/Application Support/ru-anki/whispercpp/whisper.cpp"))
_WCPP_BIN = os.environ.get("RU_WHISPERCPP_BIN",
                           os.path.join(_WCPP_DIR, "build", "bin", "whisper-cli"))
_WCPP_MODEL = os.environ.get(
    "RU_WHISPERCPP_MODEL",
    os.path.join(_WCPP_DIR, "models", "ggml-large-v3-turbo.bin"))
_WCPP_THREADS = os.environ.get("RU_WHISPERCPP_THREADS", "6")
# "mlx" (default) or "whispercpp"
_BACKEND = os.environ.get("RU_WHISPER_BACKEND", "mlx").lower()
_CPU_MODEL = os.environ.get("RU_WHISPER_CPU_MODEL", "large-v3")  # faster-whisper worker
# The parallel CPU worker adds ~25% throughput but pulls a ~1.5 GB model and runs
# the CPUs hot — opt in with RU_WHISPER_CPU_WORKER=1.
_CPU_WORKER = os.environ.get("RU_WHISPER_CPU_WORKER", "0") == "1"
_VAD = os.environ.get("RU_WHISPER_VAD", "1") != "0"
_CPU_THREADS = int(os.environ.get("RU_WHISPER_CPU_THREADS", "4"))


def _seg_wav(src, start, length, out):
    subprocess.run(
        [ytdlp.FFMPEG, "-nostdin", "-y", "-ss", str(start), "-t", str(length),
         "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", out],
        capture_output=True, text=True, timeout=600, check=True)


def _duration(src):
    r = subprocess.run(
        [ytdlp.FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", src],
        capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- models (resident)

_mlx_lock = threading.Lock()
_mlx_ready = False
_fw_model = None
_fw_lock = threading.Lock()


def _load_mlx():
    """mlx_whisper.load_model is itself lru_cached; this just populates it once
    and holds the import so the first real transcribe pays no load cost."""
    global _mlx_ready
    if not _mlx_ready:
        from mlx_whisper.load_models import load_model
        load_model(MODEL)
        _mlx_ready = True


def _load_fw():
    global _fw_model
    if _fw_model is None:
        from faster_whisper import WhisperModel
        _fw_model = WhisperModel(_CPU_MODEL, device="cpu", compute_type="int8",
                                 cpu_threads=_CPU_THREADS)
    return _fw_model


def warm():
    """Preload the MLX model (call on server startup / before a known job)."""
    try:
        _load_mlx()
    except Exception as e:  # noqa: BLE001
        print(f"[whisper] warm failed: {e}")


# ---------------------------------------------------------------- VAD

def _speech_seconds(wav):
    """Total detected speech in a WAV, or None if VAD is unavailable."""
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_speech_timestamps
    except Exception:  # noqa: BLE001
        return None
    try:
        audio = decode_audio(wav, sampling_rate=16000)
        segs = get_speech_timestamps(audio)
        return sum(s["end"] - s["start"] for s in segs) / 16000.0
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- transcription

def _mlx_segment(wav):
    from mlx_whisper import transcribe as _t
    with _mlx_lock:
        r = _t(wav, path_or_hf_repo=MODEL, language="ru",
               word_timestamps=False, condition_on_previous_text=True)
    return [(s["start"], s["end"], (s.get("text") or "").strip())
            for s in r.get("segments", [])]


def _fw_segment(wav):
    m = _load_fw()
    with _fw_lock:
        segs, _ = m.transcribe(wav, language="ru", condition_on_previous_text=True,
                               vad_filter=_VAD)
        return [(s.start, s.end, (s.text or "").strip()) for s in segs]


# ---------------------------------------------------------------- whisper.cpp

def _whispercpp_ok():
    return (_BACKEND == "whispercpp"
            and os.path.exists(_WCPP_BIN) and os.path.exists(_WCPP_MODEL))


def _transcribe_whispercpp(src, progress=None):
    """One whisper-cli process over the whole file — it does its own 30-s
    windowing with context carry-over and (with a CoreML build) runs the encoder
    through CoreML."""
    wav = f"{src}.wcpp.wav"
    out = f"{src}.wcpp"
    _seg_wav(src, 0, 10 ** 7, wav)
    try:
        proc = subprocess.Popen(
            [_WCPP_BIN, "-m", _WCPP_MODEL, "-f", wav, "-l", "ru", "-nt",
             "-oj", "-of", out, "-pp", "-t", str(_WCPP_THREADS)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for line in proc.stderr:
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m and progress:
                progress(int(m.group(1)) / 100.0)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"whisper-cli exited {proc.returncode}")
        with open(out + ".json", encoding="utf-8") as f:
            data = json.load(f)
    finally:
        for p in (wav, out + ".json"):
            if os.path.exists(p):
                os.remove(p)
    cues = []
    for s in data.get("transcription", []):
        t = (s.get("text") or "").strip()
        o = s.get("offsets") or {}
        if t and "from" in o:
            cues.append((round(o["from"] / 1000.0, 2),
                         round(o["to"] / 1000.0, 2), t))
    if progress:
        progress(1.0)
    return cues


def transcribe(src, duration=0, progress=None):
    """`src` = a local audio/video file. -> [(start_sec, end_sec, text)].
    `progress(frac)` fires as passes complete."""
    if _whispercpp_ok():
        try:
            return _transcribe_whispercpp(src, progress)
        except Exception as e:  # noqa: BLE001
            print(f"[whisper] whisper.cpp failed ({e}); falling back to MLX")

    _load_mlx()
    dur = duration or _duration(src)
    n = max(1, math.ceil(dur / _CHUNK)) if dur else 1

    want_cpu = _CPU_WORKER and n > 1
    if want_cpu:
        try:
            _load_fw()
        except Exception as e:  # noqa: BLE001
            print(f"[whisper] no CPU worker ({e}); GPU only")
            want_cpu = False

    results = [None] * n
    seg_len = _CHUNK if dur else 10 ** 6
    work = queue.Queue(maxsize=3)              # (i, wav) ready to transcribe
    counter = {"n": 0}
    clock = threading.Lock()

    def bump():
        with clock:
            counter["n"] += 1
            if progress:
                progress(min(1.0, counter["n"] / n))

    def extractor():
        for i in range(n):
            wav = f"{src}.seg{i}.wav"
            try:
                _seg_wav(src, i * _CHUNK, seg_len, wav)
            except Exception as e:  # noqa: BLE001
                print(f"[whisper] seg {i} extract failed: {e}")
                results[i] = []
                bump()
                continue
            if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
                for j in range(i, n):          # ran past the real end
                    results[j] = results[j] or []
                    bump()
                break
            work.put((i, wav))
        work.put(None)

    def worker(fn, tag):
        while True:
            item = work.get()
            if item is None:
                work.put(None)                 # let the sibling worker see it too
                return
            i, wav = item
            try:
                sp = _speech_seconds(wav) if _VAD else None
                if sp is not None and sp < 1.0:
                    results[i] = []
                else:
                    off = i * _CHUNK
                    results[i] = [(round(off + s, 2), round(off + e, 2), t)
                                  for s, e, t in fn(wav) if t]
            except Exception as ex:  # noqa: BLE001
                print(f"[whisper] {tag} seg {i} failed: {ex}")
                results[i] = results[i] or []
            finally:
                if os.path.exists(wav):
                    os.remove(wav)
            bump()

    ex = threading.Thread(target=extractor, daemon=True)
    ex.start()
    threads = [threading.Thread(target=worker, args=(_mlx_segment, "gpu"), daemon=True)]
    if want_cpu:
        threads.append(threading.Thread(target=worker, args=(_fw_segment, "cpu"),
                                        daemon=True))
    for t in threads:
        t.start()
    ex.join()
    for t in threads:
        t.join()

    cues = []
    for part in results:
        cues.extend(part or [])
    cues.sort(key=lambda c: c[0])
    return cues


def _ts(x):
    h = int(x // 3600)
    m = int(x % 3600 // 60)
    s = x % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def cues_to_vtt(cues):
    out = ["WEBVTT", "Language: ru", ""]
    for s, e, t in cues:
        out.append(f"{_ts(s)} --> {_ts(max(e, s + 0.5))}")
        out.append(t)
        out.append("")
    return "\n".join(out)
