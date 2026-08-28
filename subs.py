"""Turn a raw subtitle file (.vtt / .srt) into clean, timestamped transcript
lines.

Two input shapes:
  * YouTube auto-captions — rolling cues where each repeats the previous tail
    plus a bit of new text, carrying inline `<c>` word timings. We keep only the
    `<c>`-timed line of each cue (the genuinely new words).
  * Plain subtitles (YouTube manual captions, VK, RuTube, ripped SRT, …) — one
    self-contained cue per caption, no `<c>` markup. Handled by the `_plain_*`
    fallback used whenever the file contains no `<c>` tags at all.
"""
import html
import re

_TAG = re.compile(r"<[^>]+>")
_CUE = re.compile(r"(\d\d:\d\d:\d\d\.\d\d\d) -->")
_CUE2 = re.compile(r"(\d\d):(\d\d):(\d\d\.\d\d\d) --> (\d\d):(\d\d):(\d\d\.\d\d\d)")
# plain VTT or SRT timing line — SRT uses ',' before millis, VTT '.'; hours optional
_CUE_ANY = re.compile(
    r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d\d):(\d\d)[.,](\d{1,3})")


def _secs(h, m, s):
    return int(h) * 3600 + int(m) * 60 + float(s)


def _hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _plain_cues(raw):
    """Every self-contained caption in a plain VTT/SRT -> [(start, end, text)] in
    float seconds, de-duplicated against an immediately-repeated line."""
    out = []
    for block in re.split(r"\r?\n\r?\n", raw):
        lines = [ln for ln in block.strip("\r\n").split("\n") if ln.strip()]
        ti = next((i for i, ln in enumerate(lines) if _CUE_ANY.search(ln)), None)
        if ti is None:
            continue
        g = _CUE_ANY.search(lines[ti])
        s = _secs(g.group(1) or 0, g.group(2), f"{g.group(3)}.{g.group(4):<03s}")
        e = _secs(g.group(5) or 0, g.group(6), f"{g.group(7)}.{g.group(8):<03s}")
        body = " ".join(lines[ti + 1:])
        text = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", body))).strip()
        if not text:
            continue
        if out and out[-1][2] == text:          # some feeds repeat each cue
            out[-1] = (out[-1][0], max(out[-1][1], e), text)
            continue
        out.append((s, e, text))
    return out


def caption_cues(raw_vtt: str):
    """De-overlapped cues for the in-app player. `s`..`e` is the display window
    (each cue shown until the next one starts, so the transcript stays synced);
    `re` is the cue's *real* end, used to fade the overlay out during a silent
    gap instead of leaving a line hanging on screen."""
    if "<c>" not in raw_vtt:
        raw = [list(c) for c in _plain_cues(raw_vtt)]      # [start, real_end, text]
    else:
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
            text = re.sub(r"\s+", " ",
                          html.unescape(_TAG.sub("", with_timing[-1]))).strip()
            if not text:
                continue
            raw.append([_secs(*m.group(1, 2, 3)), _secs(*m.group(4, 5, 6)), text])

    out = []
    for i, (s, re_, t) in enumerate(raw):
        disp_end = raw[i + 1][0] if i + 1 < len(raw) else re_
        out.append({"s": round(s, 2), "e": round(disp_end, 2),
                    "re": round(re_, 2), "text": t})
    return out


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
    if "<c>" not in raw_vtt:
        return [(_hms(s), t) for s, _, t in _plain_cues(raw_vtt)]
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
