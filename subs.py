"""Turn a raw YouTube auto-caption .vtt into clean, timestamped transcript lines.

YouTube auto-captions come as rolling two-line cues where each cue repeats the
tail of the previous one plus a bit of new text carrying inline word timings.
We keep only the cues with inline `<c>` word timings, strip the markup, and get
one line of genuinely new text per cue with its start timestamp.
"""
import html
import re

_TAG = re.compile(r"<[^>]+>")
_CUE = re.compile(r"(\d\d:\d\d:\d\d\.\d\d\d) -->")
_CUE2 = re.compile(r"(\d\d):(\d\d):(\d\d\.\d\d\d) --> (\d\d):(\d\d):(\d\d\.\d\d\d)")


def _secs(h, m, s):
    return int(h) * 3600 + int(m) * 60 + float(s)


def caption_cues(raw_vtt: str):
    """De-overlapped cues with real start/end seconds, for the in-app player.
    Each cue's text is shown from its own start until the next cue's start."""
    raw = []
    for block in raw_vtt.split("\n\n"):
        lines = block.strip("\n").split("\n")
        if not lines:
            continue
        m = _CUE2.match(lines[0])
        if not m:
            continue
        with_timing = [ln for ln in lines[1:] if "<c>" in ln]
        if not with_timing:
            continue
        text = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", with_timing[-1]))).strip()
        if not text:
            continue
        raw.append([_secs(*m.group(1, 2, 3)), _secs(*m.group(4, 5, 6)), text])
    for i in range(len(raw) - 1):
        raw[i][1] = raw[i + 1][0]
    return [{"s": round(a, 2), "e": round(b, 2), "text": t} for a, b, t in raw]


def clean_transcript(raw_vtt: str):
    """-> list of (timestamp 'HH:MM:SS', text)."""
    out = []
    for block in raw_vtt.split("\n\n"):
        lines = block.strip("\n").split("\n")
        if not lines:
            continue
        m = _CUE.match(lines[0])
        if not m:
            continue
        body = "\n".join(lines[1:])
        if "<c>" not in body:
            continue
        text = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", body))).strip()
        if text:
            out.append((m.group(1)[:8], text))
    return out


def transcript_text(raw_vtt: str) -> str:
    return "\n".join(f"[{ts}] {t}" for ts, t in clean_transcript(raw_vtt))


def new_text_cues(raw_vtt: str):
    """De-overlapped transcript: only the genuinely new words each cue adds.

    In a rolling auto-caption cue the carried-over text sits on its own line(s)
    and the new words are on the line carrying the inline `<c>` word timings.
    Keeping only that line drops ~45% of the text (pure repetition) — a big win
    when feeding a multi-hour transcript to the model.
    -> list of (timestamp 'HH:MM:SS', text)
    """
    out = []
    for block in raw_vtt.split("\n\n"):
        lines = block.strip("\n").split("\n")
        if not lines:
            continue
        m = _CUE.match(lines[0])
        if not m:
            continue
        with_timing = [ln for ln in lines[1:] if "<c>" in ln]
        if not with_timing:
            continue
        text = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", with_timing[-1]))).strip()
        if text:
            out.append((m.group(1)[:8], text))
    return out


def extraction_text(raw_vtt: str, group: int = 4) -> str:
    """De-overlapped transcript, `group` short cues joined per line with a single
    leading timestamp — compact and sentence-like, for the extraction prompt."""
    cues = new_text_cues(raw_vtt)
    rows = []
    for i in range(0, len(cues), group):
        chunk = cues[i:i + group]
        rows.append(f"[{chunk[0][0]}] " + " ".join(t for _, t in chunk))
    return "\n".join(rows)
