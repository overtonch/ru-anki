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

import re as _re

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import anki  # noqa: E402
import backup  # noqa: E402
import llm  # noqa: E402
import store  # noqa: E402
import subs  # noqa: E402
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
    """One-off, in the background: fill in channel/thumbnail for old rows, and
    re-index subtitle_lines to the current de-overlapped form."""
    for v in store.videos_missing_meta():
        try:
            m = ytdlp.fetch_meta(v["url"])
            store.set_video_meta(v["id"], m.get("channel"), m.get("channel_url"),
                                 m.get("thumbnail_url"), m.get("duration"))
            print(f"[meta] backfilled video {v['id']}: {m.get('channel')}")
        except Exception as e:  # noqa: BLE001
            print(f"[meta] backfill failed for video {v['id']}: {e}")
    for v in store.list_videos():
        try:
            full = store.get_video(v["id"])
            lines = ytdlp.subtitle_lines(full["raw_subs"])
            store.replace_subtitle_lines(v["id"], lines)
        except Exception as e:  # noqa: BLE001
            print(f"[reindex] video {v['id']} failed: {e}")


threading.Thread(target=_startup_maintenance, daemon=True).start()
threading.Thread(target=llm.prewarm, daemon=True).start()

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


# ------------------------------------------------------------------ models

class VideoIn(BaseModel):
    url: str


class DecisionIn(BaseModel):
    decision: str  # "yes" | "no"


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


_EXTRACT_KEYS = ("state", "phase", "chunks_done", "chunks_total", "added",
                 "elapsed", "detail", "errors", "model", "usage")


def _extract_view(video_id):
    st = EXTRACT_STATUS.get(video_id)
    if not st:
        return None
    return {k: st[k] for k in _EXTRACT_KEYS if k in st}


@app.get("/videos")
def videos():
    out = store.list_videos()
    for v in out:
        ex = _extract_view(v["id"])
        if ex:
            v["extract"] = ex
    return out


@app.delete("/videos/{video_id}")
def delete_video(video_id: int):
    if not store.get_video(video_id):
        raise HTTPException(404, "no such video")
    EXTRACT_STATUS.pop(video_id, None)
    n = store.delete_video(video_id)
    backup.snapshot_async("delete-video")
    return {"deleted": n}


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
    if DOWNLOAD_STATUS.get(video_id, {}).get("state") == "running":
        raise HTTPException(409, "already downloading")
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


@app.get("/videos/{video_id}/media")
def media(video_id: int):
    v = store.get_video(video_id)
    if not v or not v.get("media_path") or not os.path.exists(v["media_path"]):
        raise HTTPException(404, "not downloaded")
    return FileResponse(v["media_path"], media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes",
                                 "Cache-Control": "no-store"})


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
        added = {"n": 0}

        def on_chunk(items):
            for it in items:
                it["sentence"] = store.sentence_for(
                    video_id, it.get("timestamp_start"), it["span_text"])
            got, _ = store.add_candidates(video_id, items, source="batch")
            added["n"] += len(got)

        def prog(done, total, errors):
            _set_status(video_id, state="running", phase="extracting",
                        chunks_done=done, chunks_total=total, added=added["n"],
                        errors=list(errors), elapsed=round(time.time() - t0, 1),
                        detail=(f"{done}/{total} chunks" if total else "starting"))

        items, errors, usage = llm.extract_candidates(
            v["title"], transcript, decided, model=model,
            progress=prog, on_chunk=on_chunk, discards=discards)
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
def candidates(video_id: int, status: str = "pending"):
    if status == "pending":
        store.discard_unbolded(video_id)  # never surface a broken card to the phone
    rows = store.list_candidates(video_id, status=status or None)
    v = store.get_video(video_id)
    title = v["title"] if v else ""
    have = store.card_lemmas()
    for r in rows:
        front, bolded = anki.front_html(r["sentence"], r["span_text"], r["is_phrase"])
        r["front_html"] = front
        r["bolded"] = bolded
        r["source_label"] = title
        r["duplicate"] = r["normalized_text"] in have
        r["freq"] = store.freq_hint(r["normalized_text"], r["is_phrase"])
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

    anki_result = None
    if body.decision == "yes":
        v = store.get_video(cand["video_id"])
        src = anki.source_html(v["title"] if v else "", v.get("channel") if v else None,
                               v["url"] if v else "", cand["timestamp_start"])
        try:
            anki_result = anki.add_card(
                cand["sentence"], cand["span_text"], cand["is_phrase"],
                cand["translation"], src, tags=["ru-anki", "batch"],
            )
        except anki.AnkiError as e:
            raise HTTPException(502, f"Anki: {e}")
        _sync_soon()  # don't block the response on the AnkiWeb sync

    updated = store.resolve_candidate(cand_id, body.decision)
    backup.snapshot_async("decision")
    return {"candidate": updated, "anki": anki_result}


# ------------------------------------------------------------------ live search

@app.get("/videos/{video_id}/search")
def search(video_id: int, q: str, limit: int = 40):
    if not store.get_video(video_id):
        raise HTTPException(404, "no such video")
    if len(q.strip()) < 2:
        return []
    return store.search_lines(video_id, q, limit=limit)


_CYR = _re.compile(r"[А-Яа-яЁё]")


@app.get("/videos/{video_id}/watch")
def watch(video_id: int):
    """Cues with real start/end seconds + per-word "do I have a card for this"
    flags (lemmatised server-side). Feeds the in-app player."""
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")
    cues = subs.caption_cues(v["raw_subs"])
    have = store.card_lemmas()
    pend_rows = store.list_candidates(video_id, status="pending")
    pending = {r["normalized_text"]: r["id"]
               for r in pend_rows if r.get("normalized_text")}
    out = []
    for cue in cues:
        words = []
        for tok in cue["text"].split():
            core = tok.strip(".,!?;:—–()«»\"'…-")
            w = {"t": tok, "c": False}
            if core and _CYR.search(core):
                lem = store.lemma_key(core)
                if lem in have:
                    w["c"] = True
                elif lem in pending:
                    w["p"] = pending[lem]
            words.append(w)
        out.append({"s": cue["s"], "e": cue["e"], "text": cue["text"], "words": words})
    return {
        "video": {k: v.get(k) for k in
                  ("id", "title", "channel", "url", "youtube_id", "duration")},
        "cues": out,
        "card_count": len(have),
        "cands": {r["id"]: {"span": r["span_text"], "tr": r["translation"]}
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
        "lines": [{"id": r["id"], "t": r["start_time"], "text": r["text"]} for r in rows],
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
    ctx = (sentence or "").strip() or store.sentence_for(video_id, ts, span)
    g = llm.translate_span(ctx, span)
    span_text = (g.get("span_text") or span).strip()
    is_phrase = bool(g.get("is_phrase") or len(span_text.split()) > 1)
    translation = g.get("translation") or ""
    sent = (g.get("sentence") or "").strip() or ctx
    if "\x00" not in store.bold(sent, span_text, is_phrase, "\x00"):
        sent = ctx
    front, bolded = anki.front_html(sent, span_text, is_phrase)
    return {"span_text": span_text, "is_phrase": is_phrase, "translation": translation,
            "sentence": sent, "front_html": front, "bolded": bolded, "ts": ts}


def _make_one_card(video_id, subtitle_line_id, span, timestamp=None, sentence=None,
                   span_text=None, translation=None, is_phrase=None):
    v = store.get_video(video_id)
    if not v:
        raise HTTPException(404, "no such video")

    if span_text and translation is not None:
        # already translated in the modal — skip the LLM, just build the card
        ts = _resolve_ts(video_id, subtitle_line_id, timestamp)
        sent = (sentence or "").strip() or store.sentence_for(video_id, ts, span_text)
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
    src = anki.source_html(v["title"], v.get("channel"), v["url"], ts)
    anki_result = anki.add_card(sent, span_text, ph, translation,
                                src, tags=["ru-anki", "live"])
    anki_result["sync_error"] = None
    _sync_soon()
    store.resolve_candidate(cid, "yes")
    return {"candidate_id": cid, "span_text": span_text, "is_phrase": ph,
            "translation": translation, "anki": anki_result,
            "bolded": anki_result["bolded"]}


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
