"""Speaking journal — record a monologue "about your day", code-switching to
English where you don't know the Russian. Local Whisper transcribes (auto
language, so the English survives), then one LLM pass finds mistakes, translates
the English gaps, and drafts cards.

One row per recording (`journal_sessions`); `analysis` is a JSON blob. The heavy
work (transcribe → analyse) runs in a background thread; the client polls
`GET /journal/sessions/{id}`.

Store + orchestration here; the LLM prompt is in llm.py, endpoints in main.py.
"""
import json
import os
import threading
import time

import llm
import srs
import store
import whisper_rt
import ytdlp

LEVELS = ("a2", "a2plus", "b1", "b2", "c1")


def _c():
    return store.connect()


def _level(v):
    v = (v or "b1").lower()
    return v if v in LEVELS else "b1"


def create(audio_bytes, ext=".webm", level="b1"):
    """Save the upload, make a row, kick the background pipeline. -> session id."""
    os.makedirs(ytdlp.MEDIA_DIR, exist_ok=True)
    path = os.path.join(ytdlp.MEDIA_DIR, f"journal-{int(time.time() * 1000)}{ext}")
    with open(path, "wb") as f:
        f.write(audio_bytes)
    c = _c()
    cur = c.execute(
        "INSERT INTO journal_sessions(audio_path, level, status) VALUES(?,?,'new')",
        (path, _level(level)))
    sid = cur.lastrowid
    c.commit()
    c.close()
    _start(sid)
    return sid


def _start(sid):
    threading.Thread(target=_run, args=(sid,), daemon=True).start()


def _set(sid, **cols):
    if not cols:
        return
    sets = ", ".join(f"{k}=?" for k in cols)
    c = _c()
    c.execute(f"UPDATE journal_sessions SET {sets} WHERE id=?", (*cols.values(), sid))
    c.commit()
    c.close()


def _run(sid):
    c = _c()
    row = c.execute("SELECT * FROM journal_sessions WHERE id=?", (sid,)).fetchone()
    c.close()
    if not row:
        return
    row = dict(row)
    try:
        _set(sid, status="transcribing")
        segs = whisper_rt.transcribe(row["audio_path"], 0, language=None)
        transcript = " ".join(s[2].strip() for s in segs if s[2].strip()).strip()
        if len(transcript) < 4:
            _set(sid, status="error", error="couldn't make out any speech",
                 transcript=transcript)
            return
        _set(sid, status="analyzing", transcript=transcript)
        analysis = llm.journal_analysis(transcript, level=row["level"])
        _set(sid, status="done", analysis=json.dumps(analysis, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        print(f"[journal] session {sid} failed: {e}", flush=True)
        _set(sid, status="error", error=str(e)[:300])


def _public(row):
    row = dict(row)
    an = {}
    if row.get("analysis"):
        try:
            an = json.loads(row["analysis"]) or {}
        except (ValueError, TypeError):
            an = {}
    cards = an.get("cards") or []
    return {
        "id": row["id"], "status": row["status"], "error": row.get("error"),
        "level": row["level"], "duration": row.get("duration"),
        "transcript": row.get("transcript") or "",
        "created_at": row["created_at"],
        "segments": an.get("segments") or [],
        "general": an.get("general") or "",
        "cards": [_card(x) for x in cards],
    }


def _card(x):
    return {
        "front": (x.get("front") or "").strip(),
        "back": (x.get("back") or "").strip(),
        "note": (x.get("note") or "").strip(),
        "kind": "gap" if (x.get("kind") or "").lower() == "gap" else "fix",
        "leverage": "high" if (x.get("leverage") or "").lower() == "high" else "med",
        "target": [str(t).strip() for t in (x.get("target") or []) if str(t).strip()],
    }


def get(sid):
    c = _c()
    row = c.execute("SELECT * FROM journal_sessions WHERE id=?", (sid,)).fetchone()
    c.close()
    return _public(row) if row else None


def recent(limit=30):
    c = _c()
    rows = c.execute(
        "SELECT id, status, level, created_at, substr(transcript,1,120) preview "
        "FROM journal_sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def make_cards(sid, cards):
    """cards: [{front, back, note?, kind?, target?}] the user confirmed."""
    made = []
    for x in cards or []:
        f, b = (x.get("front") or "").strip(), (x.get("back") or "").strip()
        if not (f and b):
            continue
        meta = {"kind": "journal",
                "target": [str(t).strip() for t in (x.get("target") or []) if str(t).strip()]}
        card = srs.create_production_card(
            f, b, note=(x.get("note") or "").strip() or None,
            speak_ref=f"journal:{sid}", meta=meta)
        made.append(card)
    return made
