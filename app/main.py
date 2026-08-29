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
import whisper_rt  # noqa: E402
import store  # noqa: E402
import subs  # noqa: E402
import web  # noqa: E402
import ytdlp  # noqa: E402

store.init_db()


def _startup_backup():
    try:
        backup.snapshot("startup")
    except Exception as e:  # noqa: BLE001
        print(f"[backup] startup snapshot failed: {e}")


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
            full = store.get_video(v["id"])
            store.replace_subtitle_lines(v["id"], ytdlp.subtitle_lines(full["raw_subs"]))
            print(f"[reindex] video {v['id']}: indexed")
        except Exception as e:  # noqa: BLE001
            print(f"[reindex] video {v['id']} failed: {e}")


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


def _learn_accent(span_text, sentence=""):
    """Fill the stress/ё hint for a freshly carded word (single words only)."""
    lemma = store.lemma_key((span_text or "").strip())
    if not lemma or " " in lemma:
        return
    acc = store.accent_for(lemma)
    if not acc:
        try:
            acc = llm.accent_word(store.yo_form(lemma), sentence)
            if acc:
                store.set_accent(lemma, acc)
                print(f"[accent] {lemma} -> {acc}")
        except Exception as e:  # noqa: BLE001
            print(f"[accent] {lemma}: {e}")
            return
    if acc:
        # write it onto the card(s) that triggered this — create_card stores
        # accented=None when the hint isn't cached yet.
        try:
            srs.set_accent_for_lemma(lemma, acc)
        except Exception as e:  # noqa: BLE001
            print(f"[accent] card backfill {lemma}: {e}")


def _learn_accent_async(span_text, sentence=""):
    threading.Thread(target=_learn_accent, args=(span_text, sentence),
                     daemon=True).start()


def _commit_card(*, sentence, span_text, normalized_text, is_phrase, translation,
                 source_html, candidate_id=None, video_id=None, timestamp=None,
                 accented=None, tags=None):
    """Create the in-app SRS card, and — only if the Anki dual-write setting is
    on — the Anki note too. Returns (srs_card_dict, anki_result_or_None)."""
    anki_result = None
    if srs.anki_dual_write():
        try:
            anki_result = anki.add_card(sentence, span_text, is_phrase, translation,
                                        source_html, tags=tags or ["ru-anki"],
                                        accented=accented)
        except anki.AnkiError as e:
            raise HTTPException(502, f"Anki: {e}")
        _sync_soon()
    card = srs.create_card(
        sentence, span_text, normalized_text, is_phrase, translation,
        candidate_id=candidate_id, accented=accented, video_id=video_id,
        timestamp=timestamp, anki_note_id=(anki_result or {}).get("note_id"))
    return card, anki_result


def _accent_sync(span_text, sentence, is_phrase):
    """Stress/ё hint for a card being made right now (deliberate, latency-OK).
    Cache hit is instant; a miss is one ~2s warm call. Phrases get nothing."""
    if is_phrase:
        return None
    lem = store.lemma_key(span_text)
    acc = store.accent_for(lem)
    if not acc:
        try:
            acc = llm.accent_word(store.yo_form(lem), sentence)
            store.set_accent(lem, acc)
        except Exception as e:  # noqa: BLE001
            print(f"[accent] {lem}: {e}")
    return acc


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


threading.Thread(target=_backfill_families, kwargs={"delay": 20}, daemon=True).start()


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
    video_id: int
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
    for v in out:
        ex = _extract_view(v["id"])
        if ex:
            v["extract"] = ex
        if archived:
            v["card_count"] = srs.list_cards(video=v["id"], limit=1)["total"]
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
        transcript = ytdlp.transcript_block(v["raw_subs"])
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
            accented=store.accent_for(cand["normalized_text"]),
            tags=["ru-anki", "batch"])

    updated = store.resolve_candidate(
        cand_id, body.decision,
        note_id=(anki_result or {}).get("note_id") if body.decision == "yes" else None)
    if body.decision == "yes":
        _learn_family_async(cand["normalized_text"])
        _learn_accent_async(cand["span_text"], sent)
    backup.snapshot_async("decision")
    return {"candidate": updated, "anki": anki_result, "srs_card": card}


@app.get("/words/{lemma}")
def word_detail(lemma: str):
    """Everything about one word: card status, family, and every place it's
    spoken across all your videos. `lemma` may be an inflected form."""
    lem = store.lemma_key(lemma)
    have = store.card_lemmas()
    fam_lemmas = store.known_family_lemmas()
    cand, members = store.word_status(lem)
    status = ("carded" if lem in have
              else "family" if lem in fam_lemmas
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


@app.post("/words/{lemma}/discard")
def word_discard(lemma: str):
    """'Not a word I'm learning' — stop highlighting it everywhere (breaks any
    word-family link, records it as known) and delete any pipeline-made Anki
    cards for it."""
    res = store.discard_word(store.lemma_key(lemma))
    for nid in res.get("removed_notes", []):
        anki.delete_note(nid)
    if res.get("removed_notes"):
        _sync_soon()
    res["removed_srs_cards"] = srs.delete_cards_for_lemma(res["lemma"])
    backup.snapshot_async("discard-word")
    return res


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


def _study_card_view(card, with_preview=True):
    """Trim an srs card dict to what the review screen needs."""
    if not card:
        return None
    v = store.get_video(card["video_id"]) if card.get("video_id") else None
    return {
        "id": card["id"], "front_html": card["front_html"],
        "translation": card["translation"], "span_text": card["span_text"],
        "normalized_text": card["normalized_text"], "accented": card["accented"],
        "is_new": card["is_new"], "reps": card["reps"], "lapses": card["lapses"],
        "video_id": card["video_id"],
        "video_title": v["title"] if v else None,
        "timestamp": card["timestamp"],
        "seconds": _hms_secs(card["timestamp"]) if card.get("timestamp") else None,
        "preview": srs.preview(card["id"]) if with_preview else None,
    }


@app.get("/srs/stats")
def srs_stats():
    s = srs.stats()
    s["anki_dual_write"] = srs.anki_dual_write()
    s["new_per_day"] = srs.new_per_day()
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
    cards = srs.queue(limit=limit)
    return {"cards": [_study_card_view(c) for c in cards], **srs.stats()}


@app.get("/srs/offline")
def srs_offline(days: int = 2):
    """Everything the phone needs to run review sessions with no connection for
    the next `days`: the cards (with per-card `due` / `due_now`) and the list of
    audio-clip + frame URLs to pre-download."""
    b = srs.offline_bundle(days=max(0, min(14, days)))
    cards, media = [], []
    for c in b["cards"]:
        v = _study_card_view(c)
        v["due"] = c.get("due")
        v["due_now"] = bool(c.get("due_now"))
        if v.get("seconds") is not None and v.get("video_id") is not None:
            t = round(v["seconds"])
            w = _urlparse.quote(c.get("normalized_text") or "")
            v["clip"] = f"/videos/{v['video_id']}/clip?t={t}&w={w}"
            v["frame"] = f"/videos/{v['video_id']}/frame?t={t}"
            media += [v["clip"], v["frame"]]
        cards.append(v)
    return {"generated_at": b["generated_at"], "days": b["days"],
            "cards": cards, "media": media, **srs.stats()}


@app.get("/srs/cards/{card_id}")
def srs_card(card_id: int):
    card = srs.get_card(card_id)
    if not card:
        raise HTTPException(404, "no such card")
    return {**_study_card_view(card), "preview": srs.preview(card_id)}


@app.get("/srs/cards/{card_id}/preview")
def srs_card_preview(card_id: int):
    return srs.preview(card_id)


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
def srs_delete(card_id: int, requeue: bool = False):
    """Drop a study card. By default the source word is also marked known so it
    won't be re-suggested; `?requeue=1` instead puts it back in the review queue
    (for 'the card is wrong, let me remake it')."""
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
                store.resolve_candidate(cand_id, "no")
        except KeyError:
            pass
    elif card:
        store.discard_word(store.norm(card["normalized_text"]))
    backup.snapshot_async("srs-delete")
    return {"ok": True, "requeued": requeue, **srs.stats()}


@app.post("/srs/backfill")
def srs_backfill(video_id: int | None = None, limit: int | None = None):
    n = srs.backfill_from_candidates(video_id=video_id, limit=limit)
    return {"created": n, **srs.stats()}


def _backfill_accents(limit=None):
    """Fill srs_cards.accented for single-word cards that never got a stress
    hint (review-swipe cards, pre-feature imports). Cache first, LLM for the
    rest. Runs in a background thread."""
    rows = srs.cards_missing_accent(limit=limit)
    print(f"[accent] backfilling {len(rows)} cards…")
    done = 0
    for r in rows:
        lem = store.lemma_key(r["span_text"])
        if not lem or " " in lem:
            continue
        acc = store.accent_for(lem)
        if not acc:
            try:
                acc = llm.accent_word(store.yo_form(lem), r["sentence"] or "")
                if acc:
                    store.set_accent(lem, acc)
            except Exception as e:  # noqa: BLE001
                print(f"[accent] {lem}: {e}")
                continue
        if acc and srs.set_accent_for_lemma(r["normalized_text"], acc):
            done += 1
    print(f"[accent] backfill done: {done} cards updated")


@app.post("/srs/backfill-accents")
def srs_backfill_accents(background: BackgroundTasks, limit: int | None = None):
    background.add_task(_backfill_accents, limit)
    return {"queued": srs.count_missing_accent()}


@app.get("/srs/export")
def srs_export():
    path = os.path.join(ytdlp.MEDIA_DIR, "ru-anki-srs.apkg")
    n = srs.export_apkg(path)
    return FileResponse(path, filename="ru-anki-srs.apkg", media_type="application/octet-stream",
                        headers={"X-Card-Count": str(n)})


@app.get("/settings")
def get_settings():
    return {"anki_dual_write": srs.anki_dual_write(),
            "new_per_day": srs.new_per_day()}


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
    cues = subs.caption_cues(v["raw_subs"])          # same list the player indexes
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
def post_settings(body: SettingIn):
    if body.key == "anki_dual_write":
        srs.set_setting(body.key, bool(body.value))
    elif body.key == "new_per_day":
        srs.set_setting(body.key, max(0, min(999, int(body.value))))
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
    """-> (flag(text) -> [word dict], pend_rows). Shared by /watch and /read."""
    have = store.card_lemmas() | store.known_family_lemmas()
    glosses = store.carded_glosses()
    pend_rows = store.list_candidates(video_id, status="pending")
    pending = {r["normalized_text"]: r["id"]
               for r in pend_rows if r.get("normalized_text")}
    decided = store.video_decided_lemmas(video_id)

    def flag(text):
        words = []
        for tok in text.split():
            core = tok.strip(".,!?;:—–()«»\"'…-")
            w = {"t": tok, "c": False}
            if core and _CYR.search(core):
                lem = store.lemma_key(core)
                if lem in have:
                    w["c"] = True
                    if lem in glosses:
                        w["tr"] = glosses[lem]
                elif lem in pending:
                    w["p"] = pending[lem]
                d = decided.get(lem)
                if d and d["status"] == "card_created":
                    w["cc"] = d["id"]
                elif d and d["status"] == "discarded":
                    w["dd"] = d["id"]
            words.append(w)
        return words, len(have)

    return flag, pend_rows


@app.get("/videos/{video_id}/watch")
def watch(video_id: int):
    """Cues with real start/end seconds + per-word "do I have a card for this"
    flags (lemmatised server-side). Feeds the in-app player."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    cues = subs.caption_cues(v["raw_subs"])
    flag, pend_rows = _word_flagger(video_id)
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
        "cands": {r["id"]: {"span": r["span_text"], "tr": r["translation"],
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
    flag, pend_rows = _word_flagger(video_id)
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
    return {
        "video": {k: v.get(k) for k in ("id", "title", "channel", "url")},
        "chapters": chapters, "blocks": blocks,
        "cands": {r["id"]: {"span": r["span_text"], "tr": r["translation"],
                            "freq": store.freq_hint(r["normalized_text"], r["is_phrase"])}
                  for r in pend_rows},
    }


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
    front, bolded = anki.front_html(sent, span_text, is_phrase)
    return {"span_text": span_text, "is_phrase": is_phrase, "translation": translation,
            "sentence": sent, "front_html": front, "bolded": bolded, "ts": ts,
            "stressed": (g.get("stressed") or "").strip() or None,
            "gloss": store.gloss_for(span) or store.gloss_for(span_text),
            "freq": store.freq_hint(store.lemma_key(span_text), is_phrase)}


def _make_one_card(video_id, subtitle_line_id, span, timestamp=None, sentence=None,
                   span_text=None, translation=None, is_phrase=None):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")

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

    cid = store.create_candidate(video_id, span_text, ph, sent, ts,
                                 translation, source="live")
    accented = _accent_sync(span_text, sent, ph)
    src = anki.source_html(v["title"], v.get("channel"), v["url"], ts)
    card, anki_result = _commit_card(
        sentence=sent, span_text=span_text, normalized_text=span_text,
        is_phrase=ph, translation=translation, source_html=src, candidate_id=cid,
        video_id=video_id, timestamp=ts, accented=accented, tags=["ru-anki", "live"])
    if anki_result is not None:
        anki_result["sync_error"] = None
    store.resolve_candidate(cid, "yes",
                            note_id=(anki_result or {}).get("note_id"))
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
    front, bolded = anki.front_html(sent, span_text, is_phrase)
    return {"span_text": span_text, "is_phrase": is_phrase, "translation": translation,
            "sentence": sent, "front_html": front, "bolded": bolded,
            "stressed": (g.get("stressed") or "").strip() or None,
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


@app.post("/texts/{text_id}/card")
def text_card(text_id: int, body: TextCardIn):
    t = store.get_text(text_id)
    if not t:
        raise HTTPException(404, "no such text")
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
    accented = _accent_sync(span_text, sent, ph)
    src = anki.source_html_text(t["title"], t.get("author"), body.chapter)
    card, res = _commit_card(
        sentence=sent, span_text=span_text, normalized_text=span_text,
        is_phrase=ph, translation=translation, source_html=src,
        accented=accented, tags=["ru-anki", "reading"])
    store.mark_carded(span_text)
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


@app.post("/cards/flush")
def cards_flush(body: FlushIn):
    """Batch-create queued cards (offline client reconnecting). Returns a
    per-item result so the client can drop the successful ones from its queue."""
    out = []
    for it in body.items:
        try:
            r = _make_one_card(it.video_id, it.subtitle_line_id, it.span,
                               it.timestamp, it.sentence, it.span_text,
                               it.translation, it.is_phrase)
            out.append({"client_id": it.client_id, "ok": True, **r})
        except HTTPException as e:
            out.append({"client_id": it.client_id, "ok": False, "error": str(e.detail)})
        except anki.AnkiError as e:
            out.append({"client_id": it.client_id, "ok": False, "error": f"Anki: {e}"})
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
