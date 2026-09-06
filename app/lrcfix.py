"""Automatic LRC/audio sync correction for song cards.

LRCLIB synced lyrics are matched to *some* release of a track; the copy we
actually stream (a YouTube rip found by search) is often a few seconds off, and
usually by a near-constant amount. This module transcribes the real audio with
Whisper, matches transcript lines to lyric lines by their text, and takes the
robust median of the per-line time differences to recover that constant offset,
then applies it at the source (store.shift_song_timing) so every downstream
consumer — clip windows, the karaoke player, cards — is corrected with no
read-time plumbing.

Runs once when a song is ingested (main._song_pipeline) and can be re-run any
time via POST /songs/{id}/fix-audio-sync.
"""
import os
import re
import statistics

import store
import ytdlp
import whisper_rt

_WORD = re.compile(r"[а-яёa-z0-9]+", re.I)

_MAX_SHIFT = 15.0        # ignore matches that would imply a bigger offset than this
_MIN_SIM = 0.45          # text similarity for a transcript/lyric line to count as "the same line"
_EARLY_ONLY = 105.0      # only trust timestamps from the first ~1:45 (Whisper drifts later)
_MIN_MATCHES = 6         # need at least this many confident matches
_MAX_SPREAD = 1.0        # median abs deviation of the deltas; higher => not a clean constant offset
_MAX_DRIFT = 2.0         # if the fitted delta line moves more than this across the song, it's tempo drift
_WHISPER_LEAD = 0.3      # Whisper starts a sung line a hair before its lyric onset


def _toks(s):
    return {w for w in _WORD.findall((s or "").lower().replace("ё", "е")) if len(w) > 2}


def _sim(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _lyric_lines(video_id):
    """-> [(start_sec, token_set, raw_text)] for each timed lyric line."""
    c = store.connect()
    rows = c.execute(
        "SELECT start_time, text FROM subtitle_lines WHERE video_id=? ORDER BY id",
        (video_id,)).fetchall()
    c.close()
    out = []
    for r in rows:
        s = store._to_secs(r["start_time"])
        if s is not None and (r["text"] or "").strip():
            out.append((s, _toks(r["text"])))
    return out


def _deltas(lyrics, segs):
    """For each transcript segment, the time gap to the best-matching lyric line
    (whisper_time - lyric_time), for matches confident enough to trust."""
    out = []
    for st, stoks in segs:
        best_sim, best_ls = 0.0, None
        for ls, ltoks in lyrics:
            if abs(ls - st) > _MAX_SHIFT:
                continue
            sim = _sim(stoks, ltoks)
            if sim > best_sim:
                best_sim, best_ls = sim, ls
        if best_ls is not None and best_sim >= _MIN_SIM:
            out.append((st, best_sim, st - best_ls))
    return out


def estimate_offset(video_id):
    """-> {ok, offset, ...}. `offset` = seconds to ADD to the stored lyric times
    to line them up with the audio (positive => lyrics currently run early)."""
    v = store.get_video(video_id)
    if not v:
        return {"ok": False, "reason": "no such video"}
    audio = ytdlp.audio_path(video_id)
    if not audio or not os.path.exists(audio):
        return {"ok": False, "reason": "no audio file for this song"}
    lyrics = _lyric_lines(video_id)
    if len(lyrics) < 5:
        return {"ok": False, "reason": f"only {len(lyrics)} lyric lines"}

    try:
        raw = whisper_rt.transcribe(audio, v.get("duration") or 0)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"transcribe failed: {e}"}
    segs = [(float(s) - _WHISPER_LEAD, _toks(t)) for s, _e, t in raw if _toks(t)]
    if len(segs) < 5:
        return {"ok": False, "reason": f"only {len(segs)} usable transcript lines"}

    matches = _deltas(lyrics, segs)
    early = [(st, d) for (st, sim, d) in matches if st <= _EARLY_ONLY]
    used = early if len(early) >= _MIN_MATCHES else [(st, d) for (st, _s, d) in matches]
    n = len(used)
    if n < _MIN_MATCHES:
        return {"ok": False, "n_matches": n,
                "reason": f"only {n} lyric lines matched the transcript"}

    off0 = statistics.median([d for _t, d in used])
    # drop matches that sit way off the median — a repeated chorus line paired
    # with the wrong repetition, mostly. What's left is the real alignment.
    inliers = sorted((t, d) for t, d in used if abs(d - off0) <= 2.5)
    if len(inliers) < _MIN_MATCHES:
        return {"ok": False, "n_matches": len(inliers),
                "reason": f"only {len(inliers)} consistent lyric matches"}

    off = statistics.median([d for _t, d in inliers])
    spread = statistics.median([abs(d - off) for _t, d in inliers])

    # a real constant offset is flat across the song; a tempo / structure
    # mismatch drifts. Compare the first third of the song to the last third.
    k = max(2, len(inliers) // 3)
    head = statistics.median([d for _t, d in inliers[:k]])
    tail = statistics.median([d for _t, d in inliers[-k:]])
    drift = abs(tail - head)

    ok = spread <= _MAX_SPREAD and drift <= _MAX_DRIFT
    if spread > _MAX_SPREAD:
        reason = f"time gaps inconsistent (±{spread:.1f}s) — not a clean offset"
    elif drift > _MAX_DRIFT:
        reason = (f"lyrics drift ~{drift:.0f}s against the audio across the song — "
                  f"a shift won't fix it; try re-syncing or a different source")
    else:
        reason = ""
    return {
        "ok": ok,
        "offset": round(off, 2),
        "spread": round(spread, 2),
        "drift": round(drift, 2),
        "n_matches": len(inliers),
        "n_raw": n,
        "reason": reason,
    }


def _slope(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def autofix(video_id, min_shift=0.75):
    """Estimate the offset and, if it's confident and worth doing, apply it.
    Idempotent: the stored videos.lrc_offset means a re-run only corrects the
    residual. -> the estimate dict + `applied` (seconds) + `total_offset`."""
    est = estimate_offset(video_id)
    est["applied"] = 0.0
    if est.get("ok") and abs(est["offset"]) >= min_shift:
        est["total_offset"] = store.shift_song_timing(video_id, est["offset"])
        est["applied"] = est["offset"]
    else:
        v = store.get_video(video_id)
        est["total_offset"] = (v or {}).get("lrc_offset") or 0.0
    return est
