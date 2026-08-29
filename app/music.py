"""Songs — pull synced lyrics for a track and turn them into timed cues.

Lyrics come from **LRCLIB** (https://lrclib.net) — a free, key-less, community
lyrics database with time-synced (`.lrc`) lyrics for a large Russian catalogue.
We send it only the artist + track name + duration parsed from the video's
public metadata; nothing about the user. If LRCLIB has no good match the caller
falls back to Whisper on the audio (see main._song_pipeline).
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

LRCLIB = "https://lrclib.net/api"
_UA = "ru-anki/1.0 (+https://github.com/overtonch/ru-anki)"

# "(Official Video)", "[Official Audio]", "(Lyric Video)", "(Премьера клипа)", …
_NOISE = re.compile(
    r"""\s*[\(\[\{]\s*
        (?:[^)\]\}]*\b(?:official|lyric[s]?|audio|video|visuali[sz]er|mv|m/v|
           hd|4k|clip|музыка|премьера|клип|песня|текст|караоке|karaoke|
           remaster(?:ed)?|remix|live|cover|acoustic|version|explicit|
           20\d\d)\b[^)\]\}]*)
        \s*[\)\]\}]""",
    re.I | re.X,
)
_FEAT = re.compile(r"\s*(?:\bfeat\.?|\bft\.?|\bwith\b|при участии|feat\b)\s+.+$", re.I)
_SEPS = (" — ", " – ", " -- ", " - ", " — ", "—", "–", " ~ ", " // ", " | ", " · ")


def parse_artist_title(video_title, uploader=None):
    """Best-effort (artist, title) from a video title like
    'Земфира — Искала (Official Video)'. Falls back to the uploader as artist."""
    t = (video_title or "").strip()
    t = _NOISE.sub("", t)
    t = _FEAT.sub("", t).strip(" \t-–—|·•\"'«»")
    for sep in _SEPS:
        if sep in t:
            a, b = t.split(sep, 1)
            a, b = a.strip(" \"'«»“”"), b.strip(" \"'«»“”")
            if a and b:
                return a, b
    m = re.match(r'^(.+?)\s*[«"“](.+?)[»"”]\s*$', t)          # Artist «Title»
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (uploader or "").strip(), t


def _cyrillic_fraction(s):
    letters = [c for c in (s or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "Ѐ" <= c <= "ӿ") / len(letters)


def _get(path, params):
    url = f"{LRCLIB}/{path}?" + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v})
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def fetch_lyrics(artist, title, duration=None):
    """-> {"source": "lrclib"|"lrclib-plain"|None, "synced": str|None,
           "plain": str|None, "matched": "Artist — Title"|None}.

    Prefers a result that is (a) synced, (b) mostly Cyrillic, (c) close to the
    video's duration."""
    cands = []
    if artist and title:
        try:
            cands.append(_get("get", {"artist_name": artist, "track_name": title,
                                      "duration": int(duration) if duration else None}))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            pass
    try:
        q = " ".join(x for x in (artist, title) if x)
        found = _get("search", {"q": q})
        if isinstance(found, list):
            cands.extend(found)
    except Exception:  # noqa: BLE001
        pass

    best, best_score = None, -1.0
    for c in cands:
        if not isinstance(c, dict) or c.get("instrumental"):
            continue
        synced = (c.get("syncedLyrics") or "").strip()
        plain = (c.get("plainLyrics") or "").strip()
        if _cyrillic_fraction(synced or plain) < 0.5:
            continue
        score = 100.0 if synced else 0.0
        if duration and c.get("duration"):
            score += max(0.0, 45.0 - abs(float(c["duration"]) - float(duration)))
        if score > best_score:
            best, best_score = c, score

    if not best:
        return {"source": None, "synced": None, "plain": None, "matched": None}
    matched = f'{best.get("artistName") or artist} — {best.get("trackName") or title}'
    if (best.get("syncedLyrics") or "").strip():
        return {"source": "lrclib", "synced": best["syncedLyrics"],
                "plain": best.get("plainLyrics"), "matched": matched}
    return {"source": "lrclib-plain", "synced": None,
            "plain": best.get("plainLyrics"), "matched": matched}


_LRC_TS = re.compile(r"\[(\d{1,2}):(\d{2}(?:[.:]\d{1,3})?)\]")
_META_TAG = re.compile(r"^\[[a-z]{1,8}:.*\]$", re.I)


def lrc_to_cues(lrc, total=None):
    """`.lrc` text -> [(start, end, text)] sorted by start. A line's end is the
    next line's start (blank LRC lines mark verse gaps and are dropped as cues
    but still bound the previous line)."""
    stamped = []
    for raw in (lrc or "").splitlines():
        line = raw.strip()
        if not line or (_META_TAG.match(line) and not _LRC_TS.search(line)):
            continue
        times = _LRC_TS.findall(line)
        if not times:
            continue
        text = _LRC_TS.sub("", line).strip()
        for mm, ss in times:
            secs = int(mm) * 60 + float(ss.replace(":", "."))
            stamped.append((round(secs, 2), text))
    stamped.sort(key=lambda r: r[0])

    cues = []
    for i, (start, text) in enumerate(stamped):
        nxt = stamped[i + 1][0] if i + 1 < len(stamped) else (
            total if total and total > start else start + 4.0)
        end = max(start + 0.8, min(nxt, start + 12.0))
        if text:
            cues.append((start, round(end, 2), text))
    return cues


def plain_to_cues(plain, total=None):
    """No timing available — spread the lyric lines evenly over the track so the
    player still scrolls roughly in step. Rough, but better than a wall of text."""
    lines = [ln.strip() for ln in (plain or "").splitlines() if ln.strip()]
    if not lines:
        return []
    span = float(total) if total and total > 10 else len(lines) * 3.5
    step = span / len(lines)
    return [(round(i * step, 2), round((i + 1) * step, 2), ln)
            for i, ln in enumerate(lines)]
