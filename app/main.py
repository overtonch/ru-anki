"""FastAPI server for the Russian vocab -> Anki pipeline.

Run:  ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
(from the repo root, /Users/charlie/ru-anki)
"""
import asyncio
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse as _urlparse

import html as _html
import random as _random
import re as _re

import httpx

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import anki  # noqa: E402
import backup  # noqa: E402
import epub  # noqa: E402
import heartbeat  # noqa: E402
import llm  # noqa: E402
import music  # noqa: E402
import srs  # noqa: E402
import tts  # noqa: E402
import whisper_rt  # noqa: E402
import store  # noqa: E402
import subs  # noqa: E402
import web  # noqa: E402
import wordstate  # noqa: E402
import ytdlp  # noqa: E402

store.init_db()

# RU_TEST=1 (set by the test suite) skips every boot-time background job —
# git snapshots, network metadata backfill, LLM prewarm — so importing the app
# is fast and side-effect-free.
_TESTING = os.environ.get("RU_TEST") == "1"


def _startup_backup():
    try:
        backup.snapshot("startup")
    except Exception as e:  # noqa: BLE001
        print(f"[backup] startup snapshot failed: {e}")


if not _TESTING:
    threading.Thread(target=_startup_backup, daemon=True).start()  # never block boot
    backup.start_scheduler()


def _startup_maintenance():
    """Background: fill in channel/thumbnail for old rows. Re-index
    subtitle_lines only for videos that have none (the de-overlap format is
    stable now — no need to rewrite identical rows on every boot)."""
    for v in store.videos_missing_meta():
        try:
            m = ytdlp.fetch_meta(v["url"])
            store.set_video_meta(v["id"], m.get("channel"), m.get("channel_url"),
                                 m.get("thumbnail_url"), m.get("duration"))
            print(f"[meta] backfilled video {v['id']}: {m.get('channel')}")
        except Exception as e:  # noqa: BLE001
            print(f"[meta] backfill failed for video {v['id']}: {e}")
    for v in store.list_videos():
        if v.get("lines"):
            continue
        try:
            store.replace_subtitle_lines(
                v["id"], ytdlp.subtitle_lines(store.raw_subs(v["id"])))
            print(f"[reindex] video {v['id']}: indexed")
        except Exception as e:  # noqa: BLE001
            print(f"[reindex] video {v['id']} failed: {e}")


if not _TESTING:
    threading.Thread(target=_startup_maintenance, daemon=True).start()
    threading.Thread(target=llm.prewarm, daemon=True).start()
    heartbeat.start()  # no-op unless RU_HEARTBEAT_URL is set

app = FastAPI(title="ru-anki pipeline")
app.add_middleware(GZipMiddleware, minimum_size=1000)

# extraction progress, in-memory: video_id -> {"state","detail"}
EXTRACT_STATUS = {}

# Debounced AnkiWeb sync: a burst of card creations triggers one sync ~2.5s after
# the last one, so the decision/make-card responses don't wait on the sync.
_sync_timer = None
_sync_lock = threading.Lock()


def _sync_soon():
    global _sync_timer
    with _sync_lock:
        if _sync_timer:
            _sync_timer.cancel()
        _sync_timer = threading.Timer(2.5, _do_sync)
        _sync_timer.daemon = True
        _sync_timer.start()


def _do_sync():
    err = anki.try_sync()
    if err:
        print(f"[sync] {err}")


def _stress_forms(span_text, sentence=""):
    """(surface_stressed, dict_stressed) for a word — the form as it appears in
    the sentence, and its stressed dictionary/citation form. One warm LLM call
    (the dict form is cached in word_accent). ('', '') for phrases / failure."""
    st = (span_text or "").strip()
    if not st or " " in st:
        return "", ""
    try:
        surf, df = llm.stress_forms([(st, sentence)])[0]
    except Exception as e:  # noqa: BLE001
        print(f"[stress] {st}: {e}")
        return "", store.accent_for(st) or ""
    surf, df = (surf or "").strip(), (df or "").strip()
    if df:
        store.set_accent(st, df)
        store.set_accent(df, df)          # also findable under the dict form itself
    return surf, df


def _learn_accent(span_text, sentence=""):
    """Async: compute both stressed forms for a freshly carded word and write
    them onto its card(s) (create_card leaves them NULL on the fast path)."""
    surf, df = _stress_forms(span_text, sentence)
    if not (surf or df):
        return
    try:
        srs.set_accents_for_lemma(store.norm(span_text), surf, df)
    except Exception as e:  # noqa: BLE001
        print(f"[stress] card backfill {span_text}: {e}")


def _learn_accent_async(span_text, sentence=""):
    threading.Thread(target=_learn_accent, args=(span_text, sentence),
                     daemon=True).start()


def _commit_card(*, sentence, span_text, normalized_text, is_phrase, translation,
                 source_html, candidate_id=None, video_id=None, timestamp=None,
                 accented=None, dict_accented=None, tags=None, source=None):
    """Create the in-app SRS card, and — only if the Anki dual-write setting is
    on — the Anki note too. Returns (srs_card_dict, anki_result_or_None)."""
    anki_result = None
    if srs.anki_dual_write():
        try:
            anki_result = anki.add_card(sentence, span_text, is_phrase, translation,
                                        source_html, tags=tags or ["ru-anki"],
                                        accented=dict_accented or accented)
        except anki.AnkiError as e:
            raise HTTPException(502, f"Anki: {e}")
        _sync_soon()
    card = srs.create_card(
        sentence, span_text, normalized_text, is_phrase, translation,
        candidate_id=candidate_id, accented=accented, dict_accented=dict_accented,
        video_id=video_id, timestamp=timestamp, source=source,
        anki_note_id=(anki_result or {}).get("note_id"))
    return card, anki_result


def _accent_sync(span_text, sentence, is_phrase):
    """(surface_stressed, dict_stressed) for a card being made right now
    (deliberate, latency-OK — one warm call). ('', '') for phrases."""
    if is_phrase:
        return "", ""
    return _stress_forms(span_text, sentence)


def _learn_family(lemma):
    """After a card is made, learn its word-formation family so работа/рабочий/…
    count as known too. Fire-and-forget."""
    lemma = (lemma or "").strip()
    if not lemma or " " in lemma:
        return
    if store.in_stoplist(lemma):
        return                             # never family-group a function word
    try:
        root, members = llm.word_family(lemma)
        if members:
            store.set_word_family(root or lemma, members)
            print(f"[family] {lemma} -> {root or lemma}: {len(members)} members")
    except Exception as e:  # noqa: BLE001
        print(f"[family] {lemma}: {e}")


def _learn_family_async(lemma):
    threading.Thread(target=_learn_family, args=(lemma,), daemon=True).start()


def _backfill_families(delay=0):
    if delay:
        time.sleep(delay)                  # let the server settle on startup
    todo = store.lemmas_without_family()
    if not todo:
        return
    print(f"[family] backfilling {len(todo)} carded words…")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_learn_family, todo))
    print("[family] backfill done")


def _rank_new_on_boot(delay=30):
    time.sleep(delay)
    try:
        _maybe_rank_new()
    except Exception as e:  # noqa: BLE001
        print(f"[learn] boot rank failed: {e}", flush=True)


if not _TESTING:
    threading.Thread(target=_backfill_families, kwargs={"delay": 20}, daemon=True).start()
    threading.Thread(target=_rank_new_on_boot, daemon=True).start()


# ------------------------------------------------------------------ models

class VideoIn(BaseModel):
    url: str


class SongIn(BaseModel):
    url: str


class DecisionIn(BaseModel):
    decision: str  # "yes" | "no"
    sentence: str | None = None   # override the card's context sentence


class TranslateIn(BaseModel):
    video_id: int
    span: str
    subtitle_line_id: int | None = None
    timestamp: str | None = None
    sentence: str | None = None


class MakeCardIn(BaseModel):
    subtitle_line_id: int | None = None
    span: str
    timestamp: str | None = None   # offline clients may only have this
    sentence: str | None = None    # optional client-side sentence
    # if the modal already previewed the translation, pass it back to skip the LLM
    span_text: str | None = None
    translation: str | None = None
    is_phrase: bool | None = None


class FlushItem(BaseModel):
    client_id: str
    kind: str = "video"                 # video | reader | text | manual
    video_id: int | None = None
    text_id: int | None = None
    chapter: str | None = None
    note: str | None = None
    span: str
    subtitle_line_id: int | None = None
    timestamp: str | None = None
    sentence: str | None = None
    span_text: str | None = None
    translation: str | None = None
    is_phrase: bool | None = None


class FlushIn(BaseModel):
    items: list[FlushItem]


class TextIn(BaseModel):
    title: str | None = None
    body: str


class TextTranslateIn(BaseModel):
    span: str
    sentence: str


class TextCardIn(BaseModel):
    span: str
    sentence: str
    chapter: str | None = None
    span_text: str | None = None
    translation: str | None = None
    is_phrase: bool | None = None


# ------------------------------------------------------------------ health

@app.get("/health")
def health():
    anki_ok, anki_detail = True, None
    try:
        anki.ping()
    except Exception as e:  # noqa: BLE001
        anki_ok, anki_detail = False, str(e)
    return {
        "ok": True,
        "db": store.DB,
        "videos": len(store.list_videos()),
        "anki": {"ok": anki_ok, "detail": anki_detail},
        "backup": backup.status(),
        "heartbeat": heartbeat.status(),
    }


@app.get("/backup")
def backup_status():
    return backup.status()


@app.post("/backup")
def backup_now():
    return backup.snapshot("manual")


# ------------------------------------------------------------------ videos

@app.post("/videos")
def create_video(body: VideoIn):
    """Fetch subtitles and index them. No LLM in this path — returns quickly."""
    try:
        got = ytdlp.fetch_subs(body.url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"subtitle fetch failed: {e}")
    vid = store.upsert_video(body.url, got["title"], got["subs_kind"],
                             got["subs_lang"], got["raw_subs"],
                             channel=got.get("channel"),
                             channel_url=got.get("channel_url"),
                             thumbnail_url=got.get("thumbnail_url"),
                             duration=got.get("duration"))
    lines = ytdlp.subtitle_lines(got["raw_subs"])
    n = store.replace_subtitle_lines(vid, lines)
    return {"video_id": vid, "title": got["title"],
            "subs": f"{got['subs_kind']}/{got['subs_lang']}", "lines": n}


# ------------------------------------------------------------------ songs

def _song_lyrics(artist, track, duration, sub_url=None):
    """(cues, subs_kind, note). Try LRCLIB synced lyrics, then the source video's
    own Russian subtitles, then plain LRCLIB lyrics spread over the runtime.
    Returns ([], 'pending', ...) when nothing usable was found — caller Whispers."""
    try:
        lyr = music.fetch_lyrics(artist, track, duration)
    except Exception as e:  # noqa: BLE001
        print(f"[song] lyrics lookup failed: {e}")
        lyr = {"source": None}
    if lyr.get("source") == "lrclib":
        cues = music.lrc_to_cues(lyr["synced"], duration)
        if cues:
            return cues, "lrclib", f"synced lyrics · {lyr.get('matched')}"
    if sub_url:
        try:
            got = ytdlp.fetch_subs(sub_url)
            if got.get("raw_subs"):
                cues = [(c["s"], c.get("re") or c["e"], c["text"])
                        for c in subs.caption_cues(got["raw_subs"]) if c.get("text")]
                if cues:
                    return cues, got["subs_kind"], f"{got['subs_kind']} captions"
        except Exception as e:  # noqa: BLE001
            print(f"[song] no video subs: {e}")
    if lyr.get("source") == "lrclib-plain":
        cues = music.plain_to_cues(lyr["plain"], duration)
        if cues:
            return cues, "lrclib-plain", "plain lyrics (approx timing)"
    return [], "pending", "no lyrics found — transcribing"


def _song_pipeline(video_id, url, need_whisper, model):
    """Background: audio download → (Whisper if no lyrics) → vocab extraction."""
    try:
        _run_audio_download(video_id, url)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    if need_whisper:
        _run_transcribe(video_id)
        if (TRANSCRIBE_STATUS.get(video_id) or {}).get("state") != "done":
            _set_status(video_id, state="error", phase="error",
                        detail="couldn’t transcribe the lyrics")
            return
    _run_extraction(video_id, model)


def _resolve_song_source(url):
    """(youtube_url, artist, track, duration, artwork, source_note).

    An Apple Music link is resolved to its title/artist via the iTunes API, then
    matched to a YouTube video for the audio. Anything else is treated as the
    audio source directly."""
    if music.is_apple_music(url):
        info = music.apple_lookup(url)
        if not info or not info.get("title"):
            raise HTTPException(422, "couldn’t read that Apple Music link — "
                                     "paste a link to a specific song")
        artist, track, dur = info["artist"], info["title"], info.get("duration")
        try:
            results = ytdlp.search(f"{artist} {track}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"song search failed: {e}")
        pick = music.pick_youtube(results, artist, track, dur)
        if not pick:
            raise HTTPException(404, f"couldn’t find audio for “{artist} — {track}”")
        return (pick["url"], artist, track, dur, info.get("artwork"),
                f"Apple Music · audio from YouTube ({pick['channel'] or 'unknown'})")

    try:
        meta = ytdlp.fetch_meta(url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"couldn’t read that link: {e}")
    artist, track = music.parse_artist_title(meta.get("title"), meta.get("channel"))
    return (url, artist or meta.get("channel"), track or meta.get("title"),
            meta.get("duration"), meta.get("thumbnail_url"), None)


@app.post("/songs")
def create_song(body: SongIn, background: BackgroundTasks,
                model: str = llm.DEFAULT_MODEL):
    """Add a song: resolve the link (Apple Music → iTunes lookup → YouTube audio;
    otherwise the link is the source), fetch synced lyrics, store as a
    kind='song' video, then pull audio + extract vocab in the background."""
    yt_url, artist, track, dur, artwork, src_note = _resolve_song_source(body.url)
    cues, subs_kind, note = _song_lyrics(artist, track, dur, sub_url=yt_url)
    if src_note:
        note = f"{note} · {src_note}" if note else src_note
    need_whisper = not cues
    if need_whisper:                       # placeholder line so the row is valid
        cues = [(0.0, max(dur or 4.0, 4.0), "…")]
    display = f"{artist} — {track}" if artist and track else (track or "Song")
    vid = store.add_song(yt_url, display, artist, cues, subs_kind, dur, artwork)
    if need_whisper:
        TRANSCRIBE_STATUS[vid] = {"state": "running", "pct": 0.0, "detail": "queued"}
    _set_status(vid, state="queued", phase="queued", model=model,
                chunks_done=0, chunks_total=0, added=0, errors=[], elapsed=0.0,
                detail="getting the song ready")
    background.add_task(_song_pipeline, vid, yt_url, need_whisper, model)
    return {"id": vid, "video_id": vid, "kind": "song", "title": display,
            "artist": artist, "track": track, "lyrics": subs_kind,
            "synced": not need_whisper, "note": note}


@app.post("/songs/{video_id}/refetch-lyrics")
def refetch_song_lyrics(video_id: int):
    """Re-pull the synced lyrics for a song (e.g. a mis-timed LRC) using its
    stored artist / title / duration. Keeps the audio and any cards; refreshes
    the transcript and drops the cached line explanations."""
    v = store.get_video(video_id)
    if not v or v.get("kind") != "song":
        raise HTTPException(404, "no such song")
    artist = v.get("channel") or ""
    full = v.get("title") or ""
    track = full.split("—", 1)[1].strip() if "—" in full else full
    dur = _hms_secs(v.get("duration")) if isinstance(v.get("duration"), str) else v.get("duration")
    cues, subs_kind, note = _song_lyrics(artist, track, v.get("duration"), sub_url=v["url"])
    if not cues:
        raise HTTPException(404, "still no lyrics found")
    vtt = whisper_rt.cues_to_vtt(cues)
    store.set_raw_subs(video_id, vtt, kind=subs_kind, lang="ru")
    store.replace_subtitle_lines(
        video_id, [(store.secs_to_hms(s), t) for s, _, t in cues])
    store.drop_lyric_notes(video_id)
    cand_moved = store.resnap_candidates(video_id)
    _, card_moved = srs.resnap_timestamps(video_id)
    return {"ok": True, "lines": len(cues), "subs_kind": subs_kind, "note": note,
            "last_ts": round(cues[-1][0], 1), "duration": v.get("duration"),
            "candidates_resnapped": cand_moved, "cards_resnapped": card_moved}


class SwapIn(BaseModel):
    url: str


def _run_song_swap(video_id, new_url):
    try:
        meta = ytdlp.fetch_meta(new_url)
    except Exception as e:  # noqa: BLE001
        TRANSCRIBE_STATUS[video_id] = {"state": "error", "detail": f"bad link: {e}"}
        return
    store.set_song_source(video_id, new_url, meta.get("duration"),
                          meta.get("thumbnail_url"))
    _STREAM_CACHE.pop(video_id, None)
    ap = ytdlp.audio_path(video_id)
    if ap:
        try:
            os.remove(ap)
        except OSError:
            pass
    _run_audio_download(video_id, new_url)          # pull the new audio
    v = store.get_video(video_id)
    artist = v.get("channel") or ""
    full = v.get("title") or ""
    track = full.split("—", 1)[1].strip() if "—" in full else full
    cues, subs_kind, _ = _song_lyrics(artist, track, v.get("duration"), sub_url=new_url)
    if cues:
        store.set_raw_subs(video_id, whisper_rt.cues_to_vtt(cues), kind=subs_kind, lang="ru")
        store.replace_subtitle_lines(
            video_id, [(store.secs_to_hms(s), t) for s, _, t in cues])
        store.drop_lyric_notes(video_id)
        store.resnap_candidates(video_id)
        srs.resnap_timestamps(video_id)
    TRANSCRIBE_STATUS[video_id] = {"state": "done", "detail": "new source ready"}
    backup.snapshot_async("song-swap")


@app.post("/songs/{video_id}/swap-source")
def swap_song_source(video_id: int, body: SwapIn, background: BackgroundTasks):
    """Replace a song's audio source (e.g. a censored clip → the album version).
    Re-downloads the audio and re-syncs the lyrics in the background."""
    v = store.get_video(video_id)
    if not v or v.get("kind") != "song":
        raise HTTPException(404, "no such song")
    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(422, "give a full http(s) link")
    TRANSCRIBE_STATUS[video_id] = {"state": "running", "pct": 0.0, "detail": "swapping source"}
    background.add_task(_run_song_swap, video_id, url)
    return {"ok": True, "queued": True}


_EXTRACT_KEYS = ("state", "phase", "chunks_done", "chunks_total", "added",
                 "elapsed", "detail", "errors", "model", "usage")


def _extract_view(video_id):
    st = EXTRACT_STATUS.get(video_id)
    if not st:
        return None
    return {k: st[k] for k in _EXTRACT_KEYS if k in st}


@app.get("/videos")
def videos(archived: bool = False):
    """The home list. `?archived=1` returns only the archived (hidden) videos —
    the ones whose cards were kept when the video was removed."""
    out = store.list_videos(include_hidden=True)
    out = [v for v in out if bool(v.get("hidden")) == archived]
    counts = srs.card_counts_by_video()
    for v in out:
        ex = _extract_view(v["id"])
        if ex:
            v["extract"] = ex
        v["card_count"] = counts.get(v["id"], 0)
        if v.get("kind") == "song":
            tr = TRANSCRIBE_STATUS.get(v["id"])
            if tr:
                v["transcribe"] = {"state": tr.get("state"), "detail": tr.get("detail")}
    return out


def _wipe_media_files(video_id):
    ap = ytdlp.audio_path(video_id)
    if ap:
        try:
            os.remove(ap)
        except OSError:
            pass
    import glob as _glob
    for pat in (os.path.join(ytdlp.FRAME_DIR, f"{video_id}-*.jpg"),
                os.path.join(ytdlp.CLIP_DIR, f"{video_id}-*.m4a")):
        for f in _glob.glob(pat):
            try:
                os.remove(f)
            except OSError:
                pass


@app.get("/videos/{video_id}/cards")
def video_cards(video_id: int):
    """Every study card sourced from this video — shown before you delete it so
    you can decide whether to keep them (archive the video) or delete them too."""
    if not store.get_video(video_id):
        raise HTTPException(404, "no such video")
    return srs.list_cards(video=video_id, sort="added", limit=2000)


@app.get("/videos/{video_id}/study")
def video_study(video_id: int):
    """Cards from this one piece of content, shaped for the review screen — a
    self-contained refresher. Ratings here are pure flip-through: they never call
    /review, so nothing is rescheduled and the daily queue is untouched."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    cards = srs.cards_for_video(video_id)
    _random.shuffle(cards)
    titles = store.video_titles()
    ctxs = store.card_contexts(cards)
    out = []
    for c in cards:
        cv = _study_card_view(c, with_preview=False, titles=titles)
        cv["context"] = ctxs.get(c["id"])
        out.append(cv)
    return {"cards": out, "title": v["title"], "kind": v["kind"]}


@app.delete("/videos/{video_id}")
def delete_video(video_id: int, cards: str = "keep"):
    """`cards=keep` (default) archives the video: it leaves the home list and its
    downloaded media is freed, but the row + transcript stay so the study cards
    made from it keep their jump-to-the-moment / clip / occurrences links.
    `cards=delete` removes the video *and* every study card made from it."""
    if not store.get_video(video_id):
        raise HTTPException(404, "no such video")
    EXTRACT_STATUS.pop(video_id, None)
    AUDIO_STATUS.pop(video_id, None)
    _STREAM_CACHE.pop(video_id, None)

    if cards == "delete":
        for nid in srs.anki_note_ids_for_video(video_id):
            try:
                anki.delete_note(nid)
            except anki.AnkiError:
                pass
        removed = srs.delete_cards_for_video(video_id)
        _sync_soon()
        _wipe_media_files(video_id)
        store.delete_video(video_id)
        backup.snapshot_async("delete-video")
        return {"deleted": 1, "mode": "delete", "cards_deleted": removed}

    _wipe_media_files(video_id)
    n = store.hide_video(video_id)
    backup.snapshot_async("hide-video")
    kept = srs.list_cards(video=video_id, limit=1)["total"]
    return {"deleted": n, "mode": "keep", "cards_kept": kept, "archived": True}


@app.post("/videos/{video_id}/unhide")
def unhide_video(video_id: int):
    if not store.get_video(video_id):
        raise HTTPException(404, "no such video")
    return {"ok": bool(store.unhide_video(video_id))}


# ------------------------------------------------------------------ offline media

DOWNLOAD_STATUS = {}   # video_id -> {"state","pct","detail"}


def _run_download(video_id, url, height):
    DOWNLOAD_STATUS[video_id] = {"state": "running", "pct": 0.0, "detail": "starting"}
    store.set_media(video_id, media_status="downloading")
    try:
        path, size = ytdlp.download_media(
            url, video_id, height,
            progress=lambda p: DOWNLOAD_STATUS.__setitem__(
                video_id, {"state": "running", "pct": round(p, 1),
                           "detail": f"{p:.0f}%"}))
        store.set_media(video_id, media_path=path, media_bytes=size,
                        media_quality=height, media_status="ready")
        DOWNLOAD_STATUS[video_id] = {"state": "done", "pct": 100.0,
                                     "detail": f"{size // 1_000_000} MB"}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.set_media(video_id, media_status="error")
        DOWNLOAD_STATUS[video_id] = {"state": "error", "pct": 0.0, "detail": str(e)[:200]}


@app.post("/videos/{video_id}/download")
def download(video_id: int, background: BackgroundTasks, q: int = 360):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    st = DOWNLOAD_STATUS.get(video_id, {})
    if st.get("state") == "running":          # idempotent — just report progress
        return {"video_id": video_id, **st}
    if v.get("media_status") == "ready" and v.get("media_path"):
        return {"video_id": video_id, "state": "done", "pct": 100.0}
    DOWNLOAD_STATUS[video_id] = {"state": "running", "pct": 0.0, "detail": "queued"}
    background.add_task(_run_download, video_id, v["url"], q)
    return {"video_id": video_id, "quality": q, "state": "running"}


@app.get("/videos/{video_id}/download")
def download_status(video_id: int):
    st = DOWNLOAD_STATUS.get(video_id)
    if st:
        return st
    v = store.get_video(video_id)
    return {"state": "done" if v and v.get("media_status") == "ready" else "idle",
            "pct": 100.0 if v and v.get("media_status") == "ready" else 0.0}


@app.api_route("/videos/{video_id}/media", methods=["GET", "HEAD"])
def media(video_id: int):
    v = store.get_video(video_id)
    if not v or not v.get("media_path") or not os.path.exists(v["media_path"]):
        raise HTTPException(404, "not downloaded")
    return FileResponse(v["media_path"], media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes",
                                 "Cache-Control": "no-store"})


# Proxy-stream a non-YouTube source (VK, RuTube, …) through the Mac so the phone
# can play it without downloading the whole thing. The upstream URL is IP- and
# UA-locked to whoever resolved it (the Mac), so the phone can't hit it directly.
_STREAM_CACHE = {}   # video_id -> {"url", "headers", "at"}
_STREAM_TTL = 1200


async def _resolve_stream_cached(video_id, url, force=False):
    c = _STREAM_CACHE.get(video_id)
    if not force and c and time.time() - c["at"] < _STREAM_TTL:
        return c["url"], c["headers"]
    murl, hdrs = await asyncio.to_thread(ytdlp.resolve_stream, url)
    _STREAM_CACHE[video_id] = {"url": murl, "headers": hdrs, "at": time.time()}
    return murl, hdrs


@app.api_route("/videos/{video_id}/stream", methods=["GET", "HEAD"])
async def stream(video_id: int, request: Request):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    if v.get("media_path") and os.path.exists(v["media_path"]):     # already downloaded
        return FileResponse(v["media_path"], media_type="video/mp4",
                            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"})
    try:
        murl, hdrs = await _resolve_stream_cached(video_id, v["url"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"couldn’t resolve a stream: {str(e)[:200]}")

    is_head = request.method == "HEAD"
    rng = None if is_head else request.headers.get("range")
    client = httpx.AsyncClient(follow_redirects=True,
                               timeout=httpx.Timeout(20.0, read=180.0))

    async def open_upstream(u, h):
        req_h = {k: val for k, val in h.items() if k.lower() != "range"}
        req_h["Range"] = rng or "bytes=0-"          # always range so we get 206 + real total
        return await client.send(client.build_request("GET", u, headers=req_h), stream=True)

    up = await open_upstream(murl, hdrs)
    if up.status_code in (400, 401, 403, 410, 404):        # stale signed URL — re-resolve once
        await up.aclose()
        murl, hdrs = await _resolve_stream_cached(video_id, v["url"], force=True)
        up = await open_upstream(murl, hdrs)
    if up.status_code >= 400:
        code = up.status_code
        await up.aclose(); await client.aclose()
        raise HTTPException(502, f"upstream {code}")

    out_h = {"Accept-Ranges": "bytes", "Cache-Control": "no-store",
             "Content-Type": up.headers.get("content-type", "video/mp4")}
    total = None
    cr = up.headers.get("content-range")              # "bytes a-b/total"
    if cr and "/" in cr:
        try:
            total = int(cr.rsplit("/", 1)[1])
        except ValueError:
            pass
    if rng and cr:
        out_h["Content-Range"] = cr
        out_h["Content-Length"] = up.headers.get("content-length", "")
        status = 206
    else:
        if total is not None:
            out_h["Content-Length"] = str(total)
        status = 200

    if is_head:
        await up.aclose(); await client.aclose()
        return Response(status_code=status, headers=out_h)

    async def relay():
        try:
            async for chunk in up.aiter_bytes(65536):
                yield chunk
        except (httpx.HTTPError, RuntimeError):
            pass
        finally:
            await up.aclose()
            await client.aclose()

    return StreamingResponse(relay(), status_code=status, headers=out_h)


_FRAME_SEM = asyncio.Semaphore(2)
_CLIP_SEM = asyncio.Semaphore(2)


async def _media_src(video_id, v, need_video=False):
    """(src, headers) for ffmpeg — a downloaded file if we have one, else the
    resolved stream URL. `need_video` skips the audio-only file."""
    cands = [v.get("media_path")]
    if not need_video:
        cands.append(ytdlp.audio_path(video_id))
    for p in cands:
        if p and os.path.exists(p):
            return p, None
    try:
        return await _resolve_stream_cached(video_id, v["url"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, f"no media source ({str(e)[:120]})")


@app.get("/videos/{video_id}/frame")
async def video_frame(video_id: int, t: float):
    """A single JPEG of the video at `t` seconds — the thumbnail on a review card.
    Uses the downloaded file if we have it, else the resolved stream URL."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    out = os.path.join(ytdlp.FRAME_DIR, f"{video_id}-{max(0, int(t))}.jpg")
    if not os.path.exists(out):
        src, headers = await _media_src(video_id, v, need_video=True)
        async with _FRAME_SEM:
            if not os.path.exists(out):
                try:
                    await asyncio.to_thread(ytdlp.extract_frame, src, t, out, headers)
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(502, f"frame extract failed ({str(e)[:150]})")
    return FileResponse(out, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/videos/{video_id}/clip")
async def video_clip(video_id: int, t: float, w: str = ""):
    """The listening clip for a review card. `t` is the stored (line-start)
    timestamp; `w` is the target lemma — when given, the window is snapped to
    the transcript line that actually contains the word and spans it fully."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    if w:
        start, dur = store.clip_window(video_id, w, max(0.0, t))
    else:
        start, dur = max(0.0, t - 1.5), 6.5
    out = os.path.join(ytdlp.CLIP_DIR,
                       f"{video_id}-{int(start * 10)}-{int(dur * 10)}.m4a")
    if not os.path.exists(out):
        src, headers = await _media_src(video_id, v)
        async with _CLIP_SEM:
            if not os.path.exists(out):
                try:
                    await asyncio.to_thread(ytdlp.extract_clip, src, start, dur,
                                            out, headers)
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(502, f"clip extract failed ({str(e)[:150]})")
    return FileResponse(out, media_type="audio/mp4",
                        headers={"Cache-Control": "public, max-age=604800"})


TRANSCRIBE_STATUS = {}   # video_id -> {"state","pct","detail"}


def _run_transcribe(video_id):
    v = store.get_video(video_id)
    TRANSCRIBE_STATUS[video_id] = {"state": "running", "pct": 0.0, "detail": "preparing audio"}
    try:
        src = (v["media_path"] if v.get("media_path") and os.path.exists(v["media_path"])
               else ytdlp.audio_path(video_id))
        if not src:
            TRANSCRIBE_STATUS[video_id] = {"state": "running", "pct": 0.0,
                                           "detail": "downloading audio"}
            src, _ = ytdlp.download_audio(v["url"], video_id)
        cues = whisper_rt.transcribe(
            src, v.get("duration") or 0,
            progress=lambda f: TRANSCRIBE_STATUS.__setitem__(
                video_id, {"state": "running", "pct": round(f * 100, 1),
                           "detail": f"transcribing {f * 100:.0f}%"}))
        if not cues:
            raise RuntimeError("whisper produced no transcript")
        vtt = whisper_rt.cues_to_vtt(cues)
        store.set_raw_subs(video_id, vtt, kind="whisper")
        store.replace_subtitle_lines(video_id, ytdlp.subtitle_lines(vtt))
        TRANSCRIBE_STATUS[video_id] = {"state": "done", "pct": 100.0,
                                       "detail": f"{len(cues)} lines — re-extract for fresh cards"}
        backup.snapshot_async("transcribe")
        print(f"[whisper] video {video_id}: {len(cues)} cues")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        TRANSCRIBE_STATUS[video_id] = {"state": "error", "pct": 0.0, "detail": str(e)[:200]}


@app.post("/videos/{video_id}/transcribe")
def transcribe_start(video_id: int, background: BackgroundTasks):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    st = TRANSCRIBE_STATUS.get(video_id, {})
    if st.get("state") == "running":
        return {"video_id": video_id, **st}
    TRANSCRIBE_STATUS[video_id] = {"state": "running", "pct": 0.0, "detail": "queued"}
    background.add_task(_run_transcribe, video_id)
    return {"video_id": video_id, "state": "running"}


@app.get("/videos/{video_id}/transcribe")
def transcribe_status(video_id: int):
    return TRANSCRIBE_STATUS.get(video_id) or {"state": "idle"}


AUDIO_STATUS = {}   # video_id -> {"state","pct","detail"}


def _run_audio_download(video_id, url):
    AUDIO_STATUS[video_id] = {"state": "running", "pct": 0.0, "detail": "starting"}
    try:
        _, size = ytdlp.download_audio(
            url, video_id,
            progress=lambda p: AUDIO_STATUS.__setitem__(
                video_id, {"state": "running", "pct": round(p, 1),
                           "detail": f"{p:.0f}%"}))
        AUDIO_STATUS[video_id] = {"state": "done", "pct": 100.0,
                                  "detail": f"{size // 1_000_000} MB"}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        AUDIO_STATUS[video_id] = {"state": "error", "pct": 0.0, "detail": str(e)[:200]}


@app.post("/videos/{video_id}/audio")
def audio_download(video_id: int, background: BackgroundTasks):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    if ytdlp.audio_path(video_id):
        return {"video_id": video_id, "state": "done"}
    if AUDIO_STATUS.get(video_id, {}).get("state") == "running":
        return {"video_id": video_id, "state": "running"}
    AUDIO_STATUS[video_id] = {"state": "running", "pct": 0.0, "detail": "queued"}
    background.add_task(_run_audio_download, video_id, v["url"])
    return {"video_id": video_id, "state": "running"}


@app.get("/videos/{video_id}/audio/status")
def audio_status(video_id: int):
    st = AUDIO_STATUS.get(video_id)
    if st:
        return st
    return {"state": "done" if ytdlp.audio_path(video_id) else "idle",
            "pct": 100.0 if ytdlp.audio_path(video_id) else 0.0}


@app.get("/videos/{video_id}/audio")
def audio_file(video_id: int):
    path = ytdlp.audio_path(video_id)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "not downloaded")
    return FileResponse(path, media_type="audio/mp4",
                        headers={"Accept-Ranges": "bytes",
                                 "Cache-Control": "no-store"})


@app.delete("/videos/{video_id}/audio")
def delete_audio(video_id: int):
    path = ytdlp.audio_path(video_id)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
    AUDIO_STATUS.pop(video_id, None)
    return {"ok": True}


@app.delete("/videos/{video_id}/media")
def delete_media(video_id: int):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    if v.get("media_path"):
        try:
            os.remove(v["media_path"])
        except OSError:
            pass
    store.set_media(video_id, media_path=None, media_bytes=None,
                    media_quality=None, media_status=None)
    DOWNLOAD_STATUS.pop(video_id, None)
    return {"ok": True}


@app.post("/videos/{video_id}/refresh-meta")
def refresh_meta(video_id: int):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    try:
        m = ytdlp.fetch_meta(v["url"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"metadata fetch failed: {e}")
    store.set_video_meta(video_id, m.get("channel"), m.get("channel_url"),
                         m.get("thumbnail_url"), m.get("duration"))
    return store.get_video(video_id) | {"raw_subs": None}


@app.get("/videos/{video_id}")
def video(video_id: int):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    v.pop("raw_subs", None)
    v["extract"] = _extract_view(video_id) or {"state": "idle"}
    v["counts"] = _status_counts(video_id)
    v["card_count"] = srs.list_cards(video=video_id, limit=1)["total"]
    return v


def _status_counts(video_id):
    out = {}
    for c in store.list_candidates(video_id):
        out[c["status"]] = out.get(c["status"], 0) + 1
    return out


# ------------------------------------------------------------------ extraction

def _set_status(video_id, **kw):
    EXTRACT_STATUS[video_id] = {**EXTRACT_STATUS.get(video_id, {}), **kw}


def _run_extraction(video_id, model):
    t0 = time.time()
    _set_status(video_id, state="running", phase="preparing", model=model,
                chunks_done=0, chunks_total=0, added=0, errors=[],
                elapsed=0.0, detail="reading transcript")
    try:
        v = store.get_video(video_id)
        transcript = ytdlp.transcript_block(store.raw_subs(video_id))
        decided = store.resolved_words_list()
        discards = store.recent_discards()
        recurring = store.notable_recurring(video_id)
        family = store.known_family_lemmas()
        added = {"n": 0}

        def on_chunk(items):
            for it in items:
                # prefer the model's lightly-cleaned sentence; fall back to the
                # stitched raw subtitle context if it's missing or lost the span
                s = (it.get("sentence") or "").strip()
                ph = bool(it.get("is_phrase"))
                if not s or "\x00" not in store.bold(s, it["span_text"], ph, "\x00"):
                    s = store.context_for(
                        video_id, it.get("timestamp_start"), it["span_text"])
                it["sentence"] = s
            got, _ = store.add_candidates(video_id, items, source="batch", family=family)
            added["n"] += len(got)

        def prog(done, total, errors):
            _set_status(video_id, state="running", phase="extracting",
                        chunks_done=done, chunks_total=total, added=added["n"],
                        errors=list(errors), elapsed=round(time.time() - t0, 1),
                        detail=(f"{done}/{total} chunks" if total else "starting"))

        items, errors, usage = llm.extract_candidates(
            v["title"], transcript, decided, model=model,
            progress=prog, on_chunk=on_chunk, discards=discards, recurring=recurring)
        store.discard_unbolded(video_id)
        detail = f"{added['n']} candidates ready"
        if errors:
            detail += f" · {len(errors)} chunk error(s)"
        _set_status(video_id, state="done", phase="done", added=added["n"],
                    proposed=len(items), errors=list(errors), usage=usage,
                    elapsed=round(time.time() - t0, 1), detail=detail)
        dur = v.get("duration") or 0
        print(f"[extract] video {video_id}: {usage['calls']} calls, "
              f"in={usage['in']} out={usage['out']} think={usage['think']} "
              f"est=${usage['cost_est']:.4f} for {dur//60}m{dur % 60}s video "
              f"({time.time() - t0:.0f}s wall)")
        backup.snapshot_async("extraction")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set_status(video_id, state="error", phase="error", detail=str(e),
                    elapsed=round(time.time() - t0, 1))


@app.post("/videos/{video_id}/reset-pending")
def reset_pending(video_id: int):
    """Drop every undecided suggestion for this video (before a fresh re-extract)."""
    if not store.get_video(video_id):
        raise HTTPException(404, "no such video")
    c = store.connect()
    n = c.execute("DELETE FROM candidates WHERE video_id=? AND status='pending'",
                  (video_id,)).rowcount
    c.commit()
    c.close()
    backup.snapshot_async("reset-pending")
    return {"deleted": n}


@app.post("/videos/{video_id}/extract")
def extract(video_id: int, background: BackgroundTasks, model: str = llm.DEFAULT_MODEL):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    if EXTRACT_STATUS.get(video_id, {}).get("state") in ("running", "queued"):
        raise HTTPException(409, "extraction already running for this video")
    _set_status(video_id, state="queued", phase="queued", model=model,
                chunks_done=0, chunks_total=0, added=0, errors=[], elapsed=0.0,
                detail="queued")
    background.add_task(_run_extraction, video_id, model)
    return {"video_id": video_id, "state": "queued", "model": model}


@app.get("/videos/{video_id}/extract")
def extract_status(video_id: int):
    return _extract_view(video_id) or {"state": "idle"}


@app.get("/videos/{video_id}/extract/events")
async def extract_events(video_id: int):
    """Server-sent events: push extraction progress until done/error/idle."""
    async def gen():
        yield ": open\n\n"
        last, missing = None, 0
        while True:
            view = _extract_view(video_id)
            if view is None:
                missing += 1
                if missing > 6:
                    yield f"data: {json.dumps({'state': 'idle'})}\n\n"
                    return
            else:
                missing = 0
                snap = json.dumps(view)
                if snap != last:
                    yield f"data: {snap}\n\n"
                    last = snap
                if view.get("state") in ("done", "error"):
                    return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# ------------------------------------------------------------------ candidates

@app.get("/videos/{video_id}/candidates")
def candidates(video_id: int, status: str = "pending", sort: str = "yield"):
    if status == "pending":
        store.discard_unbolded(video_id)  # never surface a broken card to the phone
    rows = store.list_candidates(video_id, status=status or None)
    v = store.get_video(video_id)
    title = v["title"] if v else ""
    have = store.card_lemmas() | store.known_family_lemmas()
    counts = store.lemma_counts(video_id)     # from the cached inverted index
    for r in rows:
        front, bolded = anki.front_html(r["sentence"], r["span_text"], r["is_phrase"])
        r["front_html"] = front
        r["bolded"] = bolded
        r["source_label"] = title
        r["duplicate"] = r["normalized_text"] in have
        r["freq"] = store.freq_hint(r["normalized_text"], r["is_phrase"])
        r["count"] = 1 if r["is_phrase"] else counts.get(r["normalized_text"], 1)
        r["sentence_count"] = r["count"]

    if sort == "yield":
        # most repeated first; among ties, the rarer word (higher rank / no rank)
        rows.sort(key=lambda r: (-r["count"], -(r["freq"].get("rank") or 10 ** 7)))
    elif sort == "rare":
        rows.sort(key=lambda r: -(r["freq"].get("rank") or 10 ** 7))
    elif sort == "common":
        rows.sort(key=lambda r: (r["freq"].get("rank") or 10 ** 7))
    # sort == "order" (or anything else): leave in extraction order
    return rows


@app.post("/candidates/{cand_id}/decision")
def decide(cand_id: int, body: DecisionIn):
    if body.decision not in ("yes", "no"):
        raise HTTPException(422, "decision must be 'yes' or 'no'")
    cand = store.get_candidate(cand_id)
    if not cand:
        raise HTTPException(404, "no such candidate")
    if cand["status"] != "pending":
        raise HTTPException(409, f"candidate already {cand['status']}")

    anki_result = card = None
    if body.decision == "yes":
        v = store.get_video(cand["video_id"])
        src = anki.source_html(v["title"] if v else "", v.get("channel") if v else None,
                               v["url"] if v else "", cand["timestamp_start"])
        sent = (body.sentence or "").strip()
        if not sent or "\x00" not in store.bold(sent, cand["span_text"],
                                                cand["is_phrase"], "\x00"):
            sent = cand["sentence"]
        elif sent != cand["sentence"]:
            store.update_candidate_sentence(cand_id, sent)
        card, anki_result = _commit_card(
            sentence=sent, span_text=cand["span_text"],
            normalized_text=cand["normalized_text"], is_phrase=cand["is_phrase"],
            translation=cand["translation"], source_html=src, candidate_id=cand_id,
            video_id=cand["video_id"], timestamp=cand["timestamp_start"],
            dict_accented=store.accent_for(cand["normalized_text"]),
            tags=["ru-anki", "batch"])

    updated = store.resolve_candidate(
        cand_id, body.decision,
        note_id=(anki_result or {}).get("note_id") if body.decision == "yes" else None)
    if body.decision == "yes":
        _learn_family_async(cand["normalized_text"])
        if not cand["is_phrase"]:
            _learn_accent_async(cand["span_text"], sent)
    backup.snapshot_async("decision")
    return {"candidate": updated, "anki": anki_result, "srs_card": card}


@app.get("/words")
def words_list(state: str):
    """Every lemma in a given verdict ('learned', 'known', …) — newest first."""
    if not wordstate.is_valid(state):
        raise HTTPException(422, f"unknown state: {state}")
    return {"state": state, "label": wordstate.label(state),
            "words": store.words_in_state(state)}


@app.get("/words/states")
def word_states():
    """The verdicts a learner can put a word in (drives the pick-a-verdict UI).
    Extend `wordstate.STATES` to add levels — nothing else here changes."""
    return wordstate.public()


@app.get("/words/{lemma}")
def word_detail(lemma: str):
    """Everything about one word: card status, family, and every place it's
    spoken across all your videos. `lemma` may be an inflected form."""
    lem = store.lemma_key(lemma)
    have = store.card_lemmas()
    fam_lemmas = store.known_family_lemmas()
    cand, members = store.word_status(lem)
    _c = store.connect()
    _vr = _c.execute("SELECT reason FROM resolved_words WHERE normalized_text=?",
                     (lem,)).fetchone()
    _c.close()
    verdict = _vr["reason"] if _vr else None
    status = ("carded" if lem in have
              else "family" if lem in fam_lemmas
              else verdict if (verdict and wordstate.is_assignable(verdict))
              else "pending" if (cand and cand["status"] == "pending")
              else "new")
    translation = (cand or {}).get("translation")
    occ = store.word_occurrences(lem)
    gloss = translation or store.word_gloss_get(lem) or store.gloss_for(lem)
    # orphaned carded word (pre-SRS Anki era) with no meaning anywhere → fill it
    if not gloss and status in ("carded", "family"):
        ctx = ""
        if occ and occ[0].get("hits"):
            ctx = occ[0]["hits"][0].get("text", "")
        try:
            g = llm.translate_span(ctx or lem, lem)
            gloss = (g.get("translation") or "").strip()
            if gloss:
                store.word_gloss_set(lem, gloss)
        except Exception as e:  # noqa: BLE001
            print(f"[gloss] {lem}: {e}")
    return {
        "lemma": lem,
        "yo": store.yo_form(lem),                 # ё-restored spelling (instant)
        "accented": store.accent_for(lem),        # stressed form or None (LLM, lazy)
        "status": status,
        "verdict": verdict if wordstate.is_assignable(verdict or "") else None,
        "translation": translation,
        "gloss": gloss,
        "family": [m for m in members if m != lem],
        "candidate_id": (cand or {}).get("id") if (cand and cand["status"] == "pending") else None,
        "videos": occ,
    }


@app.post("/words/{lemma}/accent")
def word_accent(lemma: str):
    """Compute (and cache) the stress + ё spelling for one word. Called by the
    word page after it renders, so the first open fills the hint in."""
    lem = store.lemma_key(lemma)
    acc = store.accent_for(lem)
    if not acc:
        occ = store.word_occurrences(lem)
        ctx = ""
        if occ and occ[0].get("hits"):
            ctx = occ[0]["hits"][0].get("text", "")
        try:
            acc = llm.accent_word(store.yo_form(lem), ctx)
            store.set_accent(lem, acc)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"accent lookup failed: {e}")
    return {"lemma": lem, "accented": acc}


def _apply_word_state(lemma, reason):
    """Set a lemma's verdict + tear down every card / note for it. Shared by the
    word page, the inline popovers, study, and the card list."""
    lem = store.lemma_key(lemma)
    res = store.set_word_state(lem, reason)
    n_cards, srs_notes = srs.delete_cards_for_lemma(lem)
    for nid in list(res.get("removed_notes", [])) + srs_notes:
        try:
            anki.delete_note(nid)
        except anki.AnkiError:
            pass
    if res.get("removed_notes") or srs_notes:
        _sync_soon()
    res["removed_srs_cards"] = n_cards
    backup.snapshot_async("word-state")
    return {**res, **srs.stats()}


class WordStateIn(BaseModel):
    state: str


@app.post("/words/{lemma}/state")
def word_set_state(lemma: str, body: WordStateIn):
    """Mark a word 'learned' / 'not learning' / … — stops highlighting +
    suggesting it and removes any card. `state` is a key from GET /words/states."""
    if not wordstate.is_assignable(body.state):
        raise HTTPException(422, f"not an assignable state: {body.state}")
    return _apply_word_state(lemma, body.state)


@app.delete("/words/{lemma}/state")
def word_clear_state(lemma: str):
    """Undo the verdict — the word can be suggested / highlighted again. Does
    not bring back a deleted card."""
    out = store.clear_word_state(store.lemma_key(lemma))
    backup.snapshot_async("word-state-clear")
    return {**out, **srs.stats()}


@app.post("/words/{lemma}/discard")
def word_discard(lemma: str):
    """Back-compat: same as POST /words/{lemma}/state {"state": "known"}."""
    return _apply_word_state(lemma, "known")


@app.post("/families/backfill")
def families_backfill(background: BackgroundTasks):
    todo = store.lemmas_without_family()
    background.add_task(_backfill_families)
    return {"pending": len(todo)}


@app.post("/candidates/{cand_id}/undo")
def undo_decision(cand_id: int):
    """Reverse an inline card / skip made while watching — deletes the Anki note
    if there was one and puts the candidate back to pending."""
    cand = store.get_candidate(cand_id)
    if not cand:
        raise HTTPException(404, "no such candidate")
    if cand["status"] not in ("card_created", "discarded"):
        raise HTTPException(409, f"nothing to undo (status {cand['status']})")
    was = cand["status"]
    updated, note_id = store.unresolve_candidate(cand_id)
    removed = False
    if was == "card_created":
        if srs.delete_cards_for_candidate(cand_id):
            removed = True
        if note_id:
            anki.delete_note(note_id)
            _sync_soon()
            removed = True
    backup.snapshot_async("undo")
    return {"candidate": updated, "undone": was, "card_removed": removed}


@app.get("/candidates/{cand_id}/sentences")
def candidate_sentence_options(cand_id: int, fresh: int = 0):
    """Ranked flashcard-sentence options for a candidate: every place the word is
    said in the video, each LLM-cleaned into a proper sentence, best first. The
    candidate's current sentence is included and ranked alongside.

    The result is memoised (the LLM cleaning is ~3s) and invalidated when the
    transcript or the candidate's sentence changes; `?fresh=1` forces a rebuild."""
    cand = store.get_candidate(cand_id)
    if not cand:
        raise HTTPException(404, "no such candidate")
    if not fresh:
        cached = store.get_sentence_cache(cand_id)
        if cached is not None:
            return cached
    span, ph = cand["span_text"], cand["is_phrase"]

    windows = store.candidate_windows(cand_id)
    cleaned = []
    if windows:
        try:
            cleaned = llm.clean_sentences(span, [w["raw"] for w in windows])
        except llm.LLMError:
            cleaned = [""] * len(windows)

    opts, seen = [], set()

    def add(sentence, timestamp):
        s = (sentence or "").strip()
        if not s or "\x00" not in store.bold(s, span, ph, "\x00"):
            return
        key = store.norm(s)[:60]
        if key in seen:
            return
        seen.add(key)
        opts.append({"sentence": s, "timestamp": timestamp,
                     "score": store.score_sentence(s, span)})

    add(cand["sentence"], (cand["timestamp_start"] or "")[:8])
    for w, cs in zip(windows, cleaned):
        add(cs or store._best_sentence([w["raw"]], 0, span, store._stems(span)),
            w["timestamp"])

    opts.sort(key=lambda o: -o["score"])
    for o in opts:
        o["front_html"], _ = anki.front_html(o["sentence"], span, ph)
    payload = {"span_text": span, "current": cand["sentence"], "options": opts[:8]}
    if opts:
        store.set_sentence_cache(cand_id, cand["video_id"], payload)
    return payload


# ------------------------------------------------------------------ in-app SRS

class ReviewIn(BaseModel):
    rating: int
    elapsed_ms: int | None = None


class OfflineReview(BaseModel):
    card_id: int
    rating: int
    elapsed_ms: int | None = None
    reviewed_at: str | None = None


class ReviewFlushIn(BaseModel):
    reviews: list[OfflineReview]


class SettingIn(BaseModel):
    key: str
    value: object


def _hms_secs(hms):
    m = _re.match(r"(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)", str(hms or ""))
    if not m:
        return None
    return round(int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
                 + float(m.group(3)), 2)


def _study_card_view(card, with_preview=True, titles=None):
    """Trim an srs card dict to what the review screen needs. Pass `titles`
    (store.video_titles()) when rendering many cards to avoid a query each."""
    if not card:
        return None
    vid = card.get("video_id")
    if titles is not None:
        m = titles.get(vid) or {}
        title, kind = m.get("title"), m.get("kind")
    else:
        v = store.get_video(vid) if vid else None
        title, kind = (v["title"], v["kind"]) if v else (None, None)
    seconds = _hms_secs(card["timestamp"]) if card.get("timestamp") else None
    # a text source (book / article) has no recorded audio — speak it instead
    has_clip = seconds is not None and vid is not None and kind != "text"
    clip = (f"/videos/{vid}/clip?t={round(seconds)}"
            f"&w={_urlparse.quote(card.get('normalized_text') or '')}") if has_clip \
        else (f"/srs/cards/{card['id']}/tts" if card.get("id") else None)
    # the sentence (bolded target) is always available; the front is either it or
    # just the headword, per the reversible card_front setting
    sentence_html = card["front_html"]
    mode = srs.card_front()
    if mode == "word":
        hw = (card.get("front_word") or card.get("dict_accented")
              or card.get("normalized_text") or card["span_text"] or "").strip()
        front_html = f'<div class="hw">{_html.escape(hw)}</div>'
    else:
        front_html = sentence_html
    return {
        "id": card["id"], "front_html": front_html,
        "sentence_html": sentence_html, "front_mode": mode,
        "front_word": card.get("front_word"),
        "translation": card["translation"], "span_text": card["span_text"],
        "normalized_text": card["normalized_text"], "accented": card["accented"],
        "dict_accented": card["dict_accented"],
        "is_new": card["is_new"], "reps": card["reps"], "lapses": card["lapses"],
        "video_id": vid,
        "video_title": title,
        "timestamp": card["timestamp"],
        "seconds": seconds if has_clip else None,   # only when it maps to a real clip
        "clip": clip,
        "tts": not has_clip,
        "preview": srs.preview(card) if with_preview else None,
    }


@app.get("/srs/stats")
def srs_stats():
    _maybe_rank_new()
    s = srs.stats()
    s["anki_dual_write"] = srs.anki_dual_write()
    s["new_per_day"] = srs.new_per_day()
    s["card_front"] = srs.card_front()
    return s


@app.get("/srs/analytics")
def srs_analytics(days: int = 30):
    return srs.analytics(days=max(7, min(120, days)))


@app.get("/srs/cards")
def srs_cards_list(filter: str = "all", sort: str = "added", q: str = "",
                   limit: int = 1000, video: int | None = None):
    return {**srs.list_cards(filter, sort, q.strip(), max(1, min(2000, limit)),
                             video=video),
            "filter": filter}


@app.post("/srs/cards/orphans/delete")
def srs_delete_orphans():
    """Delete every study card whose source video was hard-deleted before delete
    became a soft archive — they have no jump target, clip or way to be relinked."""
    for nid in srs.orphan_anki_note_ids():
        try:
            anki.delete_note(nid)
        except anki.AnkiError:
            pass
    n = srs.delete_orphan_cards()
    if n:
        _sync_soon()
        backup.snapshot_async("srs-delete-orphans")
    return {"deleted": n, **srs.stats()}


@app.post("/srs/resnap")
def srs_resnap(background: BackgroundTasks):
    """Re-align every card's stored timestamp to where the word is actually
    spoken in the transcript (fixes clips/jumps that were off)."""
    checked, moved = srs.resnap_timestamps()
    return {"checked": checked, "moved": moved}


class CardEditIn(BaseModel):
    sentence: str | None = None
    span_text: str | None = None
    translation: str | None = None


@app.patch("/srs/cards/{card_id}")
def srs_edit_card(card_id: int, body: CardEditIn):
    if not srs.get_card(card_id):
        raise HTTPException(404, "no such card")
    card = srs.update_card(card_id, sentence=body.sentence,
                           span_text=body.span_text, translation=body.translation)
    front, bolded = anki.front_html(card["sentence"], card["span_text"],
                                    bool(card["is_phrase"]))
    return {**_study_card_view(card, with_preview=False),
            "front_html": front, "bolded": bolded, "sentence": card["sentence"]}


@app.get("/srs/queue")
def srs_queue(limit: int = 60):
    _maybe_rank_new()
    cards = srs.queue(limit=limit)
    titles = store.video_titles()
    ctxs = store.card_contexts(cards)
    out = []
    for c in cards:
        v = _study_card_view(c, titles=titles)
        v["context"] = ctxs.get(c["id"])
        out.append(v)
    return {"cards": out, **srs.stats()}


@app.get("/srs/offline")
def srs_offline(days: int = 2):
    """Everything the phone needs to run review sessions with no connection for
    the next `days`: the cards (with per-card `due` / `due_now`) and the list of
    audio-clip + frame URLs to pre-download."""
    b = srs.offline_bundle(days=max(0, min(14, days)))
    titles = store.video_titles()
    ctxs = store.card_contexts(b["cards"])
    cards, media = [], []
    for c in b["cards"]:
        v = _study_card_view(c, titles=titles)
        v["due"] = c.get("due")
        v["due_now"] = bool(c.get("due_now"))
        v["context"] = ctxs.get(c["id"])
        if v.get("clip"):
            media.append(v["clip"])
        if v.get("seconds") is not None and v.get("video_id") is not None:
            v["frame"] = f"/videos/{v['video_id']}/frame?t={round(v['seconds'])}"
            media.append(v["frame"])
        cards.append(v)
    return {"generated_at": b["generated_at"], "days": b["days"],
            "cards": cards, "media": media, **srs.stats()}


def _card_issues(card):
    """Human-readable things that might be wrong with a card — for the detail
    view. Each: {level: 'warn'|'info', msg, fix?}."""
    out = []
    sp = card["span_text"] or ""
    sent = (card["sentence"] or "").strip()
    ph = bool(card["is_phrase"])
    _, bolded = anki.front_html(sent, sp, ph)
    if not bolded:
        out.append({"level": "warn", "fix": "recheck",
                    "msg": f"“{sp}” can’t be found in the sentence, so the card front "
                           f"won’t highlight it. Re-check will re-derive it, or edit below."})
    if not sent or sent == sp:
        out.append({"level": "info",
                    "msg": "No example sentence — the front is just the word."})
    if not (card["translation"] or "").strip():
        out.append({"level": "warn", "fix": "recheck", "msg": "No translation."})
    if not ph and not (card["accented"] or card["dict_accented"]):
        out.append({"level": "info", "msg": "No stress marks yet — the daily pass fills these in."})
    if (card.get("video_id") is None and card.get("candidate_id") is None
            and card.get("source") != "manual"):
        out.append({"level": "warn",
                    "msg": "The source video was deleted — no clip or jump-to-the-moment."})
    if len(sent) > 320:
        out.append({"level": "info", "msg": "Long sentence — consider trimming it in edit."})
    return out


@app.get("/srs/cards/{card_id}")
def srs_card(card_id: int):
    card = srs.get_card(card_id)
    if not card:
        raise HTTPException(404, "no such card")
    v = _study_card_view(card)
    v["context"] = store.card_context(card.get("video_id"), card.get("sentence") or "")
    titles = store.video_titles()
    src = titles.get(card.get("video_id")) or {}
    return {**v, "preview": srs.preview(card_id),
            "issues": _card_issues(card),
            "sentence": card["sentence"], "is_phrase": bool(card["is_phrase"]),
            "source": card.get("source"), "candidate_id": card.get("candidate_id"),
            "created_at": card.get("created_at"), "last_review": card.get("last_review"),
            "reps": card["reps"], "lapses": card["lapses"],
            "stability": card.get("stability"), "difficulty": card.get("difficulty"),
            "fsrs_state": card.get("fsrs_state"), "due": card.get("due"),
            "suspended": bool(card.get("suspended")), "learn_score": card.get("learn_score"),
            "anki_note_id": card.get("anki_note_id"),
            "video_kind": src.get("kind"), "video_title": src.get("title")}


def _recheck_card(card):
    """Re-derive a card's target + translation + stress from its own sentence —
    fixes an unmatched target / missing translation. Returns the updated card or
    None if it didn't improve."""
    span = srs._strip_stress(card["span_text"] or "")
    sent = (card["sentence"] or "").strip()
    if not span:
        return None
    try:
        p = _translate_ctx(span, sent)
    except llm.LLMError:
        return None
    new_span = p["span_text"]
    _, bolded = anki.front_html(p["sentence"] or sent, new_span, p["is_phrase"])
    upd = srs.update_card(
        card["id"],
        span_text=new_span,
        sentence=(p["sentence"] or sent) if bolded else None,
        translation=p["translation"] or card["translation"] or None)
    acc, dacc = p.get("stressed"), p.get("dict_form")
    if not p["is_phrase"]:
        srs.set_accents_for_lemma(store.norm(new_span), acc or "", dacc or "", force=True)
    return srs.get_card(card["id"])


@app.post("/srs/cards/{card_id}/recheck")
def srs_card_recheck(card_id: int):
    card = srs.get_card(card_id)
    if not card:
        raise HTTPException(404, "no such card")
    _recheck_card(card)
    _sync_soon()
    backup.snapshot_async("card-recheck")
    return srs_card(card_id)


def _fix_all_cards():
    fixed_stress = srs.strip_span_stress()
    print(f"[fix] stripped stress from {fixed_stress} span_texts", flush=True)
    rechecked = 0
    for row in srs.list_cards("all", limit=5000)["cards"]:
        if row["bolded"]:
            continue
        c = srs.get_card(row["id"])
        if c and _recheck_card(c):
            rechecked += 1
    _sync_soon()
    backup.snapshot_async("fix-all-cards")
    print(f"[fix] rechecked {rechecked} unbolded cards", flush=True)


@app.post("/srs/cards/fix")
def srs_fix_cards(background: BackgroundTasks):
    """Bulk repair: strip stray stress marks out of span_text, then re-derive
    every card whose target still can't be located in its sentence."""
    stressed = srs.strip_span_stress()   # do the cheap deterministic part now
    unbolded = sum(1 for r in srs.list_cards("all", limit=5000)["cards"] if not r["bolded"])
    if unbolded:
        background.add_task(_fix_all_cards)
    return {"span_stress_stripped": stressed, "unbolded_queued": unbolded}


@app.get("/srs/cards/{card_id}/preview")
def srs_card_preview(card_id: int):
    return srs.preview(card_id)


@app.get("/srs/cards/{card_id}/context")
def srs_card_context(card_id: int):
    """The line/paragraph before and after this card's sentence — shown on the
    card back for context, most useful for text-source cards with no clip."""
    card = srs.get_card(card_id)
    if not card:
        raise HTTPException(404, "no such card")
    return store.card_context(card.get("video_id"), card.get("sentence") or "")


@app.get("/srs/cards/{card_id}/tts")
def srs_card_tts(card_id: int):
    """Speak the card's sentence with the local macOS Russian voice — the
    "listen" clip for cards whose source is text (no recorded audio)."""
    card = srs.get_card(card_id)
    if not card:
        raise HTTPException(404, "no such card")
    text = (card.get("sentence") or card.get("span_text") or "").strip()
    try:
        path = tts.synthesize(text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"tts failed: {e}")
    return FileResponse(path, media_type="audio/mp4",
                        headers={"Accept-Ranges": "bytes",
                                 "Cache-Control": "max-age=604800"})


@app.post("/srs/cards/{card_id}/review")
def srs_review(card_id: int, body: ReviewIn):
    if body.rating not in (1, 2, 3, 4):
        raise HTTPException(422, "rating must be 1..4")
    try:
        card = srs.review(card_id, body.rating, body.elapsed_ms)
    except KeyError:
        raise HTTPException(404, "no such card")
    backup.snapshot_async("srs-review")
    return {"card": _study_card_view(card), **srs.stats()}


@app.post("/srs/reviews/flush")
def srs_reviews_flush(body: ReviewFlushIn):
    """Replay reviews queued while the phone was offline, oldest first."""
    revs = sorted(body.reviews, key=lambda r: r.reviewed_at or "")
    out = []
    for r in revs:
        if r.rating not in (1, 2, 3, 4):
            out.append({"card_id": r.card_id, "ok": False, "error": "bad rating"})
            continue
        try:
            srs.review(r.card_id, r.rating, r.elapsed_ms, at=r.reviewed_at)
            out.append({"card_id": r.card_id, "ok": True})
        except KeyError:
            out.append({"card_id": r.card_id, "ok": True, "error": "gone"})  # card deleted — drop it
        except Exception as e:  # noqa: BLE001
            out.append({"card_id": r.card_id, "ok": False, "error": str(e)[:120]})
    if any(o["ok"] for o in out):
        backup.snapshot_async("srs-flush")
    return {"results": out, **srs.stats()}


@app.post("/srs/cards/{card_id}/undo")
def srs_undo(card_id: int):
    card = srs.undo_last(card_id)
    if not card:
        raise HTTPException(409, "nothing to undo for this card")
    return {"card": {**_study_card_view(card), "preview": srs.preview(card_id)},
            **srs.stats()}


@app.post("/srs/cards/{card_id}/suspend")
def srs_suspend(card_id: int, on: bool = True):
    srs.suspend(card_id, on)
    return {"ok": True, "suspended": on, **srs.stats()}


@app.delete("/srs/cards/{card_id}")
def srs_delete(card_id: int, requeue: bool = False, verdict: str = "known"):
    """Drop a study card. `?verdict=learned|known` (default known) records the
    learner's take on the word so it isn't re-suggested; `?requeue=1` instead
    puts it back in the review queue (for 'the card is wrong, let me remake it').
    """
    if not requeue and not wordstate.is_assignable(verdict):
        raise HTTPException(422, f"not an assignable verdict: {verdict}")
    card = srs.get_card(card_id)
    if card and card.get("anki_note_id"):
        anki.delete_note(card["anki_note_id"])
        _sync_soon()
    srs.delete_card(card_id)
    cand_id = card.get("candidate_id") if card else None
    if cand_id:
        try:
            if requeue:
                store.unresolve_candidate(cand_id)
            else:
                store.resolve_candidate(cand_id, "no", reason=verdict)
        except KeyError:
            pass
    elif card and not requeue:
        store.set_word_state(store.norm(card["normalized_text"]), verdict)
    backup.snapshot_async("srs-delete")
    return {"ok": True, "requeued": requeue, "verdict": None if requeue else verdict,
            **srs.stats()}


@app.post("/srs/backfill")
def srs_backfill(video_id: int | None = None, limit: int | None = None):
    n = srs.backfill_from_candidates(video_id=video_id, limit=limit)
    return {"created": n, **srs.stats()}


def _backfill_accents(limit=None, force=False):
    """Fill srs_cards.accented (surface) + dict_accented (citation form) with
    their stress marks. `force` re-derives every single-word card; otherwise only
    ones still missing a form. Batched LLM calls, background thread."""
    rows = (srs.accent_backfill_rows() if force
            else srs.cards_missing_accent(limit=limit))
    if limit:
        rows = rows[:limit]
    print(f"[stress] backfilling {len(rows)} card lemmas (force={force})…", flush=True)
    done = 0
    BATCH = 20
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        try:
            pairs = llm.stress_forms([(r["span_text"], r.get("sentence") or "",
                                       r.get("translation") or "") for r in chunk])
        except Exception as e:  # noqa: BLE001
            print(f"[stress] batch {i}: {e}", flush=True)
            continue
        for r, (surf, df) in zip(chunk, pairs):
            surf, df = (surf or "").strip(), (df or "").strip()
            if df:
                store.set_accent(r["span_text"], df)
                store.set_accent(df, df)
            done += srs.set_accents_for_lemma(r["normalized_text"], surf, df, force=force)
    print(f"[stress] backfill done: {done} cards updated", flush=True)


@app.post("/srs/backfill-accents")
def srs_backfill_accents(background: BackgroundTasks, limit: int | None = None,
                         force: bool = False):
    background.add_task(_backfill_accents, limit, force)
    return {"queued": len(srs.accent_backfill_rows()) if force
            else srs.count_missing_accent(), "force": force}


# --- learn-first ordering: a daily batched LLM pass scores the not-yet-seen
#     cards 0-100 (higher = introduce sooner). queue() picks the day's new cards
#     by that score. Re-run each day so newly-added cards get placed and the
#     ranking can drift with the collection.
_RANK_LOCK = threading.Lock()


def _rank_new_cards(rescore_all=False):
    if not _RANK_LOCK.acquire(blocking=False):
        return
    try:
        rows = srs.cards_for_learn_ranking()
        if not rescore_all:
            rows = [r for r in rows if r.get("learn_score") is None]
        if not rows:
            srs.set_setting("learn_rank_day", srs._day_start_iso()[:10])
            return
        print(f"[learn] ranking {len(rows)} new cards (rescore_all={rescore_all})…", flush=True)
        BATCH, done = 50, 0
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            try:
                scores = llm.learn_priority(
                    [(r.get("front_word") or r["span_text"], r.get("translation") or "")
                     for r in chunk])
            except Exception as e:  # noqa: BLE001
                print(f"[learn] batch {i}: {e}", flush=True)
                continue
            mapped = {r["id"]: s for r, s in zip(chunk, scores) if s is not None}
            # anything the model skipped: park it mid-scale so it isn't stuck last
            for r in chunk:
                mapped.setdefault(r["id"], 50)
            done += srs.set_learn_scores(mapped)
        srs.set_setting("learn_rank_day", srs._day_start_iso()[:10])
        print(f"[learn] ranked {done} cards", flush=True)
    finally:
        _RANK_LOCK.release()


def _maybe_rank_new():
    """Cheap check on every queue / stats load: (re)rank when the day rolled over
    or fresh unranked cards showed up. Runs in a thread — never blocks the call."""
    if _TESTING:
        return
    today = srs._day_start_iso()[:10]
    stale_day = srs.get_setting("learn_rank_day") != today
    if not stale_day and srs.unranked_new_count() == 0:
        return
    threading.Thread(target=_rank_new_cards, kwargs={"rescore_all": stale_day},
                     daemon=True).start()


@app.post("/srs/rank-new")
def srs_rank_new(background: BackgroundTasks, rescore_all: bool = True):
    background.add_task(_rank_new_cards, rescore_all)
    return {"queued": len(srs.cards_for_learn_ranking()), "rescore_all": rescore_all}


def _backfill_front_words(force=False):
    """Fill srs_cards.front_word — the headword shown on the front in 'word'
    mode. A single word with a known stressed dict form reuses it (no LLM);
    everything else (phrases, words with no cached stress) goes through one
    batched llm.dict_forms call."""
    rows = srs.cards_for_front_word_backfill(force=force)
    print(f"[front] backfilling {len(rows)} card headwords (force={force})…", flush=True)
    done = 0
    BATCH = 20
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        need_llm = []
        for r in chunk:
            df = (r.get("dict_accented") or "").strip()
            if not r["is_phrase"] and df:
                done += srs.set_front_word(r["id"], df)
            else:
                need_llm.append(r)
        if not need_llm:
            continue
        try:
            forms = llm.dict_forms([(r["span_text"], r.get("sentence") or "")
                                    for r in need_llm])
        except Exception as e:  # noqa: BLE001
            print(f"[front] batch {i}: {e}", flush=True)
            forms = [""] * len(need_llm)
        for r, form in zip(need_llm, forms):
            fw = (form or "").strip() or (
                r["span_text"].strip() if r["is_phrase"]
                else (r.get("normalized_text") or r["span_text"] or "").strip())
            done += srs.set_front_word(r["id"], fw)
    print(f"[front] backfill done: {done} headwords set", flush=True)


@app.post("/srs/backfill-front-words")
def srs_backfill_front_words(background: BackgroundTasks, force: bool = False):
    background.add_task(_backfill_front_words, force)
    return {"queued": len(srs.cards_for_front_word_backfill(force=force)), "force": force}


@app.get("/srs/export")
def srs_export():
    path = os.path.join(ytdlp.MEDIA_DIR, "ru-anki-srs.apkg")
    n = srs.export_apkg(path)
    return FileResponse(path, filename="ru-anki-srs.apkg", media_type="application/octet-stream",
                        headers={"X-Card-Count": str(n)})


@app.get("/settings")
def get_settings():
    return {"anki_dual_write": srs.anki_dual_write(),
            "new_per_day": srs.new_per_day(),
            "card_front": srs.card_front()}


class PassageIn(BaseModel):
    text: str


@app.post("/translate/passage")
def translate_passage_ep(body: PassageIn):
    t = (body.text or "").strip()
    if len(t) < 3:
        raise HTTPException(422, "nothing to translate")
    try:
        return {"translation": llm.translate_passage(t[:4000])}
    except llm.LLMError as e:
        raise HTTPException(502, f"translation failed: {e}")


class LyricIn(BaseModel):
    index: int
    refresh: bool = False


@app.post("/songs/{video_id}/explain")
def explain_lyric_line(video_id: int, body: LyricIn):
    """Deep read of one lyric line — translation + what's being expressed +
    wordplay / entendres / references — in the context of the whole song.
    Memoised per (song, line)."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such song")
    cues = subs.caption_cues(store.raw_subs(video_id))   # same list the player indexes
    lines = [c["text"] for c in cues]
    if not (0 <= body.index < len(lines)):
        raise HTTPException(422, "line index out of range")
    if not body.refresh:
        hit = store.lyric_note_get(video_id, body.index)
        if hit:
            return {**hit, "index": body.index, "line": lines[body.index], "cached": True}
    try:
        out = llm.explain_lyric(lines[body.index], "\n".join(lines),
                                title=v.get("title") or "", artist=v.get("channel") or "")
    except llm.LLMError as e:
        raise HTTPException(502, f"couldn’t explain that line: {e}")
    if out.get("translation") or out.get("gist"):
        store.lyric_note_set(video_id, body.index, out)
    return {**out, "index": body.index, "line": lines[body.index], "cached": False}


@app.post("/settings")
def post_settings(body: SettingIn, background: BackgroundTasks):
    if body.key == "anki_dual_write":
        srs.set_setting(body.key, bool(body.value))
    elif body.key == "new_per_day":
        srs.set_setting(body.key, max(0, min(999, int(body.value))))
    elif body.key == "card_front":
        v = str(body.value)
        if v not in ("sentence", "word"):
            raise HTTPException(422, "card_front must be 'sentence' or 'word'")
        srs.set_setting(body.key, v)
        if v == "word" and srs.count_missing_front_word():
            background.add_task(_backfill_front_words)   # fill headwords lazily
    else:
        raise HTTPException(422, f"unknown setting {body.key}")
    return {"ok": True, body.key: srs.get_setting(body.key)}


# ------------------------------------------------------------------ live search

@app.get("/videos/{video_id}/search")
def search(video_id: int, q: str, limit: int = 40):
    if not store.get_video(video_id):
        raise HTTPException(404, "no such video")
    if len(q.strip()) < 2:
        return []
    return store.search_lines(video_id, q, limit=limit)


_CYR = _re.compile(r"[А-Яа-яЁё]")


def _word_flagger(video_id):
    """-> (flag(text) -> [word dict], pend_rows, seen_lemmas set). Shared by
    /watch and /read. `seen_lemmas` accumulates every Cyrillic lemma flag() has
    tokenised, so the caller can ship a gloss pack for the whole piece."""
    have = store.card_lemmas() | store.known_family_lemmas()
    glosses = store.carded_glosses()
    accents = store.carded_accents()
    pend_rows = store.list_candidates(video_id, status="pending")
    pending = {r["normalized_text"]: r["id"]
               for r in pend_rows if r.get("normalized_text")}
    decided = store.video_decided_lemmas(video_id)
    seen = set()

    def flag(text):
        words = []
        for tok in text.split():
            core = tok.strip(".,!?;:—–()«»\"'…-")
            w = {"t": tok, "c": False}
            if core and _CYR.search(core):
                lem = store.lemma_key(core)
                seen.add(lem)
                w["l"] = lem                       # for the offline gloss lookup
                if lem in have:
                    w["c"] = True
                    if lem in glosses:
                        w["tr"] = glosses[lem]
                    ac = accents.get(lem)
                    if ac:
                        if ac[0]:
                            w["ac"] = ac[0]
                        if ac[1] and ac[1] != ac[0]:
                            w["da"] = ac[1]
                elif lem in pending:
                    w["p"] = pending[lem]
                d = decided.get(lem)
                if d and d["status"] == "card_created":
                    w["cc"] = d["id"]
                elif d and d["status"] == "discarded":
                    w["dd"] = d["id"]
            words.append(w)
        return words, len(have)

    return flag, pend_rows, seen


@app.get("/videos/{video_id}/watch")
def watch(video_id: int):
    """Cues with real start/end seconds + per-word "do I have a card for this"
    flags (lemmatised server-side). Feeds the in-app player."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    cues = subs.caption_cues(store.raw_subs(video_id))
    flag, pend_rows, seen = _word_flagger(video_id)
    out = []
    card_count = 0
    for cue in cues:
        # ё -> е in the displayed transcript: reading practice happens without
        # accent marks. The real spelling lives on the card + word page.
        text = cue["text"].replace("ё", "е").replace("Ё", "Е")
        words, card_count = flag(text)
        out.append({"s": cue["s"], "e": cue["e"], "re": cue.get("re", cue["e"]),
                    "text": text, "words": words})
    return {
        "video": {k: v.get(k) for k in
                  ("id", "title", "channel", "url", "youtube_id", "duration",
                   "thumbnail_url", "kind")},
        "cues": out,
        "card_count": card_count,
        "glossary": store.glosses_for(seen),   # offline gloss pack (lemma -> EN)
        "cands": {r["id"]: {"span": r["span_text"], "tr": r["translation"],
                            "acc": store.accent_for(r["normalized_text"]),
                            "freq": store.freq_hint(r["normalized_text"], r["is_phrase"])}
                  for r in pend_rows},
    }


@app.get("/videos/{video_id}/read")
def read_text(video_id: int):
    """A kind='text' item as blocks for the reader: chapter headings + paragraphs
    of per-word tap-to-card spans (same flags as /watch)."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    c = store.connect()
    rows = c.execute("SELECT id, text FROM subtitle_lines WHERE video_id=? ORDER BY id",
                     (video_id,)).fetchall()
    c.close()
    flag, pend_rows, seen = _word_flagger(video_id)
    blocks, chapters, cn = [], [], 0
    for r in rows:
        txt = (r["text"] or "")
        if txt.startswith("## "):
            cn += 1
            title = txt[3:].strip()
            chapters.append({"n": cn, "title": title, "block": len(blocks)})
            blocks.append({"h": title})
        else:
            disp = txt.replace("ё", "е").replace("Ё", "Е")
            words, _ = flag(disp)
            blocks.append({"w": words, "line_id": r["id"]})
    src = v.get("url") or ""
    return {
        "video": {k: v.get(k) for k in ("id", "title", "channel", "url")},
        "chapters": chapters, "blocks": blocks,
        "chapters_loaded": cn,
        "glossary": store.glosses_for(seen),   # offline gloss pack (lemma -> EN)
        # a paginated online book can grow — the reader shows a "load more" button
        "expandable": bool(src) and "ilibrary.ru" in src,
        "cands": {r["id"]: {"span": r["span_text"], "tr": r["translation"],
                            "acc": store.accent_for(r["normalized_text"]),
                            "freq": store.freq_hint(r["normalized_text"], r["is_phrase"])}
                  for r in pend_rows},
    }


class MoreChaptersIn(BaseModel):
    count: int | None = 5


@app.post("/videos/{video_id}/more-chapters")
def more_chapters(video_id: int, body: MoreChaptersIn):
    """Pull the next batch of chapters for an imported paginated book and append
    them — reading position, cards and highlights are left untouched."""
    v = store.get_video(video_id)
    if not v or v.get("kind") != "text":
        raise HTTPException(404, "no such reading text")
    url = (v.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(422, "this text has no source URL to expand from")
    loaded = len(store.reading_chapters(video_id))
    want = max(1, min(12, body.count or 5))
    try:
        parsed = web.import_url(url, max_chapters=want, start=loaded)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"couldn’t fetch: {str(e)[:150]}")
    except ValueError as e:
        raise HTTPException(422, str(e))
    added = store.append_reading_chapters(video_id, parsed["chapters"],
                                          from_num=loaded + 1)
    backup.snapshot_async("more-chapters")
    total = parsed.get("total_chapters")
    new_loaded = loaded + len(parsed["chapters"])
    return {"added_chapters": len(parsed["chapters"]), "added_lines": added,
            "chapters_loaded": new_loaded,
            "more_available": bool(total and new_loaded < total),
            "total_chapters": total}


@app.get("/videos/{video_id}/lines")
def video_lines(video_id: int):
    """Everything an offline client needs: video meta + the full transcript."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    c = store.connect()
    rows = c.execute(
        "SELECT id, start_time, text FROM subtitle_lines WHERE video_id=? ORDER BY id",
        (video_id,),
    ).fetchall()
    c.close()
    meta = {k: v.get(k) for k in
            ("id", "title", "channel", "channel_url", "url", "thumbnail_url", "duration")}
    meta["lines"] = len(rows)
    return {
        "video": meta,
        "lines": [{"id": r["id"], "t": r["start_time"],
                   "text": (r["text"] or "").replace("ё", "е").replace("Ё", "Е")}
                  for r in rows],
    }


def _resolve_ts(video_id, subtitle_line_id, timestamp):
    if subtitle_line_id is not None:
        line = store.get_subtitle_line(subtitle_line_id)
        if line and line["video_id"] == video_id:
            return line["start_time"]
    return timestamp


def _pv_dict_form(g, span_text, is_phrase):
    """The stressed dictionary form for a translate preview — from the same LLM
    call if it gave one, else the cache. Caches whatever we settle on."""
    if is_phrase:
        return None
    df = (g.get("dict_form") or "").strip()
    if not df:
        df = store.accent_for(span_text) or ""
    if df:
        store.set_accent(span_text, df)
        store.set_accent(df, df)
    return df or None


def _translate_preview(video_id, span, subtitle_line_id=None, timestamp=None,
                       sentence=None):
    """Just the back-of-card content — no card is created. Fast single call."""
    ts = _resolve_ts(video_id, subtitle_line_id, timestamp)
    ctx = (sentence or "").strip() or store.context_for(video_id, ts, span)
    g = llm.translate_span(ctx, span)
    span_text = (g.get("span_text") or span).strip()
    is_phrase = bool(g.get("is_phrase") or len(span_text.split()) > 1)
    translation = g.get("translation") or ""
    sent = (g.get("sentence") or "").strip() or ctx
    if "\x00" not in store.bold(sent, span_text, is_phrase, "\x00"):
        sent = ctx
    sent = sent.replace("\N{COMBINING ACUTE ACCENT}", "").replace("\N{COMBINING GRAVE ACCENT}", "")
    front, bolded = anki.front_html(sent, span_text, is_phrase)
    df = _pv_dict_form(g, span_text, is_phrase)
    return {"span_text": span_text, "is_phrase": is_phrase, "translation": translation,
            "sentence": sent, "front_html": front, "bolded": bolded, "ts": ts,
            "stressed": (g.get("stressed") or "").strip() or None,
            "dict_form": df,
            "gloss": store.gloss_for(span) or store.gloss_for(span_text),
            "freq": store.freq_hint(store.lemma_key(span_text), is_phrase)}


def _also_card_surface_lemma(tapped, span_text, is_phrase):
    """The word as it sits in the text can lemmatise differently than the LLM's
    citation form — archaic spelling (сбирался -> сбираться, not собираться),
    a fused clitic, an OCR quirk. Record the surface form's lemma as carded too
    so the reader / watch view actually turns it green (otherwise it reverts on
    refresh and you card it again and again)."""
    if is_phrase or not tapped:
        return
    try:
        a, b = store.lemma_key(tapped.strip()), store.lemma_key((span_text or "").strip())
        if a and a != b:
            store.mark_carded(tapped)
    except Exception:  # noqa: BLE001
        pass


def _make_one_card(video_id, subtitle_line_id, span, timestamp=None, sentence=None,
                   span_text=None, translation=None, is_phrase=None):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")

    acc = dacc = None
    if span_text and translation is not None:
        # already translated in the modal — skip the LLM, just build the card
        ts = _resolve_ts(video_id, subtitle_line_id, timestamp)
        sent = (sentence or "").strip() or store.context_for(video_id, ts, span_text)
        ph = bool(is_phrase) if is_phrase is not None else len(span_text.split()) > 1
    else:
        try:
            p = _translate_preview(video_id, span, subtitle_line_id, timestamp, sentence)
        except llm.LLMError as e:
            raise HTTPException(502, f"translation failed: {e}")
        span_text, translation, ph, sent, ts = (
            p["span_text"], p["translation"], p["is_phrase"], p["sentence"], p["ts"])
        acc, dacc = p.get("stressed"), p.get("dict_form")

    cid = store.create_candidate(video_id, span_text, ph, sent, ts,
                                 translation, source="live")
    if not ph and not (acc and dacc):
        acc, dacc = _accent_sync(span_text, sent, ph)
    src = anki.source_html(v["title"], v.get("channel"), v["url"], ts)
    card, anki_result = _commit_card(
        sentence=sent, span_text=span_text, normalized_text=span_text,
        is_phrase=ph, translation=translation, source_html=src, candidate_id=cid,
        video_id=video_id, timestamp=ts, accented=acc, dict_accented=dacc,
        tags=["ru-anki", "live"])
    if anki_result is not None:
        anki_result["sync_error"] = None
    store.resolve_candidate(cid, "yes",
                            note_id=(anki_result or {}).get("note_id"))
    _also_card_surface_lemma(span, span_text, ph)
    _learn_family_async(store.lemma_key(span_text))
    return {"candidate_id": cid, "span_text": span_text, "is_phrase": ph,
            "translation": translation, "anki": anki_result, "srs_card": card,
            "bolded": card["bolded"]}


@app.get("/gloss")
def gloss(span: str):
    """Instant local-dictionary gloss (no LLM) — shown while /translate runs."""
    return {"span": span, "gloss": store.gloss_for(span)}


@app.post("/client-log")
async def client_log(req: Request):
    """The phone posts JS errors here so they land in ~/Library/Logs/ru-anki.log
    and can be inspected when a bug is reported."""
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = {}
    tag = str(b.get("tag", "?"))[:60]
    msg = " ".join(str(b.get("msg", "")).split())[:1500]
    ctx = json.dumps(b.get("ctx") or {}, ensure_ascii=False)[:600]
    ua = str(b.get("ua", ""))[:200]
    print(f"[client] {tag}: {msg} | ctx={ctx} | {ua}", flush=True)
    return {"ok": True}


# ------------------------------------------------------------------ reading

def _translate_ctx(span, ctx):
    """LLM back-of-card content for a span in a given sentence (no timestamp,
    no transcript stitching — the caller supplies the context)."""
    ctx = (ctx or "").strip()
    g = llm.translate_span(ctx, span)
    span_text = (g.get("span_text") or span).strip()
    is_phrase = bool(g.get("is_phrase") or len(span_text.split()) > 1)
    translation = g.get("translation") or ""
    sent = (g.get("sentence") or "").strip() or ctx
    if "\x00" not in store.bold(sent, span_text, is_phrase, "\x00"):
        sent = ctx
    sent = sent.replace("\N{COMBINING ACUTE ACCENT}", "").replace("\N{COMBINING GRAVE ACCENT}", "")
    front, bolded = anki.front_html(sent, span_text, is_phrase)
    return {"span_text": span_text, "is_phrase": is_phrase, "translation": translation,
            "sentence": sent, "front_html": front, "bolded": bolded,
            "stressed": (g.get("stressed") or "").strip() or None,
            "dict_form": _pv_dict_form(g, span_text, is_phrase),
            "gloss": store.gloss_for(span) or store.gloss_for(span_text),
            "freq": store.freq_hint(store.lemma_key(span_text), is_phrase)}


_CYR_W = _re.compile(r"[А-Яа-яЁё][А-Яа-яЁё-]*")


@app.get("/texts")
def texts_list():
    return store.list_texts()


@app.post("/texts")
def text_create(body: TextIn):
    if len(body.body.strip()) < 20:
        raise HTTPException(422, "text is too short")
    try:
        parsed = epub.from_plain(body.body, body.title)
    except ValueError as e:
        raise HTTPException(422, str(e))
    tid = store.add_text(parsed["title"], parsed.get("author"), "paste",
                         parsed["chapters"])
    backup.snapshot_async("add-text")
    return {"id": tid, "title": parsed["title"], "chapters": len(parsed["chapters"])}


class UrlIn(BaseModel):
    url: str
    chapters: int | None = 5


@app.post("/texts/from-url")
def text_from_url(body: UrlIn):
    """Import a web page (an online book chapter, an article) as a reading text.
    Stored as a kind='text' video so extraction / cards / word pages apply."""
    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(422, "give a full http(s) URL")
    try:
        parsed = web.import_url(url, body.chapters or 5)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"couldn’t fetch that page: {str(e)[:150]}")
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not any(ch.get("paragraphs") for ch in parsed["chapters"]):
        raise HTTPException(422, "no readable text found on that page")
    vid = store.add_reading_text(url, parsed["title"], parsed.get("author"),
                                 parsed["chapters"])
    backup.snapshot_async("import-text")
    return {"id": vid, "kind": "text", "title": parsed["title"],
            "author": parsed.get("author"),
            "chapters": len(parsed["chapters"]),
            "paragraphs": sum(len(ch.get("paragraphs") or []) for ch in parsed["chapters"])}


@app.post("/texts/upload")
async def text_upload(file: UploadFile = File(...)):
    data = await file.read()
    name = (file.filename or "").lower()
    try:
        if name.endswith(".epub") or data[:2] == b"PK":
            parsed = epub.parse(data)
            kind = "epub"
        else:
            parsed = epub.from_plain(data.decode("utf-8", "replace"),
                                     os.path.splitext(file.filename or "")[0])
            kind = "txt"
    except ValueError as e:
        raise HTTPException(422, f"couldn’t read that file: {e}")
    tid = store.add_text(parsed["title"], parsed.get("author"), kind,
                         parsed["chapters"])
    backup.snapshot_async("add-text")
    return {"id": tid, "title": parsed["title"], "author": parsed.get("author"),
            "chapters": len(parsed["chapters"])}


@app.get("/texts/{text_id}")
def text_meta(text_id: int):
    t = store.get_text(text_id)
    if not t:
        raise HTTPException(404, "no such text")
    return t


@app.get("/texts/{text_id}/chapters/{idx}")
def text_chapter(text_id: int, idx: int):
    ch = store.get_chapter(text_id, idx)
    if not ch:
        raise HTTPException(404, "no such chapter")
    have = store.card_lemmas() | store.known_family_lemmas()
    carded = sorted({
        w for w in {m.group(0) for m in _CYR_W.finditer(ch["body"])}
        if store.lemma_key(w) in have
    })
    return {**ch, "carded": carded}


@app.delete("/texts/{text_id}")
def text_delete(text_id: int):
    if not store.get_text(text_id):
        raise HTTPException(404, "no such text")
    n = store.delete_text(text_id)
    backup.snapshot_async("delete-text")
    return {"deleted": n}


@app.post("/texts/{text_id}/translate")
def text_translate(text_id: int, body: TextTranslateIn):
    if not store.get_text(text_id):
        raise HTTPException(404, "no such text")
    try:
        return _translate_ctx(body.span, body.sentence)
    except llm.LLMError as e:
        raise HTTPException(502, f"translation failed: {e}")


class ManualCardIn(BaseModel):
    span: str
    sentence: str | None = None
    note: str | None = None          # "where did you hear it" — goes on the source line


@app.post("/srs/cards")
def manual_card(body: ManualCardIn):
    """Add a card by hand — for a word/phrase heard outside the app. No video
    source; the sentence you give (if any) is the context, and the card gets
    spoken audio (TTS) like a reading card."""
    span = (body.span or "").strip()
    if not span:
        raise HTTPException(422, "give a word or phrase")
    try:
        p = _translate_ctx(span, body.sentence)
    except llm.LLMError as e:
        raise HTTPException(502, f"translation failed: {e}")
    span_text, translation, ph, sent = (
        p["span_text"], p["translation"], p["is_phrase"], p["sentence"])
    sent = (sent or "").strip() or span_text     # no context given -> front is just the word
    acc, dacc = p.get("stressed"), p.get("dict_form")
    if not ph and not (acc and dacc):
        acc, dacc = _accent_sync(span_text, sent, ph)
    card, res = _commit_card(
        sentence=sent, span_text=span_text, normalized_text=span_text,
        is_phrase=ph, translation=translation,
        source_html=anki.source_html_manual(body.note),
        accented=acc, dict_accented=dacc, source="manual",
        tags=["ru-anki", "manual"])
    store.mark_carded(span_text)
    _also_card_surface_lemma(span, span_text, ph)
    _learn_family_async(store.lemma_key(span_text))
    threading.Thread(target=_rank_new_cards, daemon=True).start()   # place it in the learn order
    backup.snapshot_async("manual-card")
    return {"span_text": span_text, "translation": translation, "is_phrase": ph,
            "accented": acc, "dict_accented": dacc,
            "srs_card": card, "anki": res, **srs.stats()}


@app.post("/texts/{text_id}/card")
def text_card(text_id: int, body: TextCardIn):
    t = store.get_text(text_id)
    if not t:
        raise HTTPException(404, "no such text")
    acc = dacc = None
    if body.span_text and body.translation is not None:
        span_text = body.span_text.strip()
        translation = body.translation
        ph = bool(body.is_phrase) if body.is_phrase is not None else " " in span_text
        sent = (body.sentence or "").strip()
        if "\x00" not in store.bold(sent, span_text, ph, "\x00"):
            sent = body.sentence or ""
    else:
        try:
            p = _translate_ctx(body.span, body.sentence)
        except llm.LLMError as e:
            raise HTTPException(502, f"translation failed: {e}")
        span_text, translation, ph, sent = (
            p["span_text"], p["translation"], p["is_phrase"], p["sentence"])
        acc, dacc = p.get("stressed"), p.get("dict_form")
    if not ph and not (acc and dacc):
        acc, dacc = _accent_sync(span_text, sent, ph)
    src = anki.source_html_text(t["title"], t.get("author"), body.chapter)
    card, res = _commit_card(
        sentence=sent, span_text=span_text, normalized_text=span_text,
        is_phrase=ph, translation=translation, source_html=src,
        accented=acc, dict_accented=dacc, tags=["ru-anki", "reading"])
    store.mark_carded(span_text)
    _also_card_surface_lemma(body.span, span_text, ph)
    _learn_family_async(store.lemma_key(span_text))
    backup.snapshot_async("text-card")
    return {"span_text": span_text, "translation": translation, "is_phrase": ph,
            "anki": res, "srs_card": card, "bolded": card["bolded"]}


@app.post("/translate")
def translate_preview(body: TranslateIn):
    """Back-of-card preview for the modal — no card created."""
    if not store.get_video(body.video_id):
        raise HTTPException(404, "no such video")
    try:
        return _translate_preview(body.video_id, body.span, body.subtitle_line_id,
                                  body.timestamp, body.sentence)
    except llm.LLMError as e:
        raise HTTPException(502, f"translation failed: {e}")


def _flush_one(it: FlushItem):
    """Create one queued card, dispatching on kind. Cards made offline arrive
    with span_text/translation blank so the server does a proper LLM pass."""
    if it.kind == "manual":
        return manual_card(ManualCardIn(span=it.span, sentence=it.sentence, note=it.note))
    if it.kind == "text" and it.text_id is not None:
        return text_card(it.text_id, TextCardIn(
            span=it.span, sentence=it.sentence or "", chapter=it.chapter,
            span_text=it.span_text, translation=it.translation, is_phrase=it.is_phrase))
    # video / reader — reader items carry the kind='text' video's id
    if it.video_id is None:
        raise HTTPException(422, "no video for this card")
    return _make_one_card(it.video_id, it.subtitle_line_id, it.span,
                          it.timestamp, it.sentence, it.span_text,
                          it.translation, it.is_phrase)


@app.post("/cards/flush")
def cards_flush(body: FlushIn):
    """Batch-create queued cards (offline client reconnecting). Returns a
    per-item result so the client can drop the successful ones from its queue."""
    out = []
    for it in body.items:
        try:
            r = _flush_one(it)
            out.append({"client_id": it.client_id, "ok": True,
                        "span_text": r.get("span_text"), "translation": r.get("translation")})
        except HTTPException as e:
            out.append({"client_id": it.client_id, "ok": False, "error": str(e.detail)})
        except anki.AnkiError as e:
            out.append({"client_id": it.client_id, "ok": False, "error": f"Anki: {e}"})
        except Exception as e:  # noqa: BLE001
            out.append({"client_id": it.client_id, "ok": False, "error": str(e)[:200]})
    if any(o["ok"] for o in out):
        backup.snapshot_async("flush")
    return {"results": out}


@app.post("/videos/{video_id}/make-card")
def make_card(video_id: int, body: MakeCardIn):
    try:
        r = _make_one_card(video_id, body.subtitle_line_id, body.span,
                           body.timestamp, body.sentence, body.span_text,
                           body.translation, body.is_phrase)
    except anki.AnkiError as e:
        raise HTTPException(502, f"Anki: {e}")
    backup.snapshot_async("make-card")
    return r


# ------------------------------------------------------------------ frontend

STATIC = os.path.join(HERE, "static")
app.mount("/app", StaticFiles(directory=STATIC, html=True), name="static")
app.mount("/icons", StaticFiles(directory=os.path.join(STATIC, "icons")), name="icons")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.json"),
                        media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
