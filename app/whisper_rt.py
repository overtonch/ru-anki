"""Local Whisper re-transcription (mlx-whisper on the Apple GPU). No API cost —
pure compute. Used to replace sketchy auto-captions or caption content that has
none. ~2-3x realtime for large-v3-turbo on this Mac, so a background job.
"""
import math
import os
import subprocess

import ytdlp

MODEL = os.environ.get("RU_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
_CHUNK = int(os.environ.get("RU_WHISPER_CHUNK", "600"))   # seconds per pass


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


def transcribe(src, duration=0, progress=None):
    """`src` = a local audio/video file. -> [(start_sec, end_sec, text)].
    `progress(frac)` fires after each ~10-min pass."""
    import mlx_whisper  # heavy import — only when actually transcribing

    dur = duration or _duration(src)
    n = max(1, math.ceil(dur / _CHUNK)) if dur else 1
    cues = []
    for i in range(n):
        wav = f"{src}.seg{i}.wav"
        try:
            _seg_wav(src, i * _CHUNK, _CHUNK if dur else 10 ** 6, wav)
            if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
                break                                   # ran past the end
            r = mlx_whisper.transcribe(
                wav, path_or_hf_repo=MODEL, language="ru",
                word_timestamps=False, condition_on_previous_text=True)
        finally:
            if os.path.exists(wav):
                os.remove(wav)
        for s in r.get("segments", []):
            t = (s.get("text") or "").strip()
            if t:
                cues.append((round(i * _CHUNK + s["start"], 2),
                             round(i * _CHUNK + s["end"], 2), t))
        if progress:
            progress((i + 1) / n)
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
