"""Speech Lab — a collection of longer pieces (a paragraph or two) to memorise,
shadow, and loop in the background.

Add one two ways: GENERATE from a brief (`llm.speech_draft`, tuned to the user's
voice), or PASTE your own (`llm.speech_annotate` translates + pulls out the key
chunks, and flags anything that sounds off). Either way the row lands with
status='audio' and a background thread builds natural Russian audio via
`tts_hq` (Silero); the client polls `GET /speeches/{id}` until status='ready'.

Store + orchestration here; prompts in llm.py, endpoints in main.py.
"""
import json
import os
import random
import threading

import llm
import speech_topics
import srs
import store
import tts_hq

_BACKENDS = ("elevenlabs", "silero")


def default_backend():
    """The TTS backend for new speeches and the plain 'rebuild' button."""
    v = srs.get_setting("speech_tts_backend",
                        "elevenlabs" if tts_hq.has_elevenlabs() else "silero")
    return v if v in _BACKENDS else "silero"


def set_default_backend(v):
    if v not in _BACKENDS:
        raise ValueError("bad backend")
    srs.set_setting("speech_tts_backend", v)
    return v


def _c():
    return store.connect()


def _spawn(fn, *args):
    """Run a background job. Patched to run synchronously in tests."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def _title_from(text):
    line = (text or "").strip().split("\n", 1)[0].strip()
    return (line[:60] or "Untitled speech")


def _clean_notes(notes):
    out = []
    for n in (notes or [])[:12]:
        if isinstance(n, dict) and n.get("ru"):
            out.append({"ru": str(n["ru"]).replace("́", "").strip(),
                        "why": str(n.get("why") or "").strip()})
    return out


def _join_paras(v):
    """The model returns paragraphs as a list (no line breaks inside JSON
    strings); pasted text comes as a plain string. Normalise both to text with
    blank lines between paragraphs."""
    if isinstance(v, (list, tuple)):
        return "\n\n".join(str(p).strip() for p in v if str(p).strip())
    return str(v or "")


def _norm_ru(v):
    return _join_paras(v).replace("́", "").replace("\r", "").strip()


def _norm_en(v):
    return _join_paras(v).replace("\r", "").strip()


# ---------------------------------------------------------------- create

def create_generated(topic, extra="", topic_id=None, title=None):
    """Insert a placeholder immediately (so the list can show it drafting), then
    run the LLM draft + audio build in the background. -> speech id."""
    brief = (topic or "").strip()
    if extra and extra.strip():
        brief = (brief + "\n\n" + extra.strip()).strip()
    c = _c()
    cur = c.execute(
        "INSERT INTO speeches(title, topic, topic_id, source, ru, notes, status) "
        "VALUES(?,?,?, 'generated', '', '{}', 'drafting')",
        ((title or _title_from(topic or "New speech"))[:120], brief or None, topic_id))
    sid = cur.lastrowid
    c.commit()
    c.close()
    _spawn(_draft_then_build, sid, topic or "", extra or "")
    return sid


# ---------------------------------------------------------------- suggest one

def suggestions(limit=8):
    """Curated topic briefs the learner hasn't generated yet — the highest-value
    (`core`) ones first, then the rest shuffled in. -> [{id, label, core}]."""
    c = _c()
    rows = c.execute(
        "SELECT DISTINCT topic_id FROM speeches WHERE topic_id IS NOT NULL").fetchall()
    c.close()
    done = {r["topic_id"] for r in rows}
    fresh = [t for t in speech_topics.TOPICS if t.id not in done]
    if not fresh:                       # done them all — offer the lot again
        fresh = list(speech_topics.TOPICS)
    core = [t for t in fresh if t.core]
    rest = [t for t in fresh if not t.core]
    random.shuffle(core)
    random.shuffle(rest)
    ordered = core + rest
    return [{"id": t.id, "label": t.label, "core": t.core} for t in ordered[:limit]]


def create_suggested(topic_id=None):
    """Generate a speech from the topic bank. With no id, pick a fresh one
    (a core topic when any remain). -> speech id, or None if the id is unknown."""
    if topic_id:
        t = speech_topics.get(topic_id)
        if not t:
            return None
    else:
        picks = suggestions(limit=99)
        if not picks:
            return None
        t = speech_topics.get(picks[0]["id"])
    return create_generated(t.brief, topic_id=t.id, title=t.label)


def _draft_then_build(sid, topic, extra):
    try:
        d = llm.speech_draft(topic, extra)
        ru = _norm_ru(d.get("ru"))
        if not ru:
            raise ValueError("empty draft")
        payload = {"notes": _clean_notes(d.get("notes"))}
        _set(sid, title=(d.get("title") or _title_from(ru)).strip()[:120],
             ru=ru, en=_norm_en(d.get("en")),
             notes=json.dumps(payload, ensure_ascii=False), status="audio")
    except Exception as e:  # noqa: BLE001
        print(f"[speech] draft failed for {sid}: {e}", flush=True)
        _set(sid, status="error", error=str(e)[:300])
        return
    _build_audio(sid)


def create_pasted(title, ru, en=""):
    ru = _norm_ru(ru)
    if not ru:
        raise ValueError("no text")
    ann = {}
    try:
        ann = llm.speech_annotate(ru)
    except Exception as e:  # noqa: BLE001
        print(f"[speech] annotate failed: {e}", flush=True)
    return _insert(title=(title or ann.get("title") or _title_from(ru)).strip(),
                   topic=None, source="pasted", ru=ru,
                   en=(_norm_en(en) or _norm_en(ann.get("en"))),
                   notes=ann.get("notes"), flags=ann.get("register_flags"))


def _insert(*, title, topic, source, ru, en, notes, flags=None):
    payload = {"notes": _clean_notes(notes)}
    if flags:
        payload["flags"] = [str(f).strip() for f in flags if str(f).strip()][:6]
    c = _c()
    cur = c.execute(
        "INSERT INTO speeches(title, topic, source, ru, en, notes, status) "
        "VALUES(?,?,?,?,?,?, 'audio')",
        (title[:120], topic, source, ru, en, json.dumps(payload, ensure_ascii=False)))
    sid = cur.lastrowid
    c.commit()
    c.close()
    _start(sid)
    return sid


# ---------------------------------------------------------------- audio

def _start(sid):
    _spawn(_build_audio, sid)


def _set(sid, **cols):
    if not cols:
        return
    c = _c()
    c.execute(f"UPDATE speeches SET {','.join(k + '=?' for k in cols)} WHERE id=?",
              (*cols.values(), sid))
    c.commit()
    c.close()


def _build_audio(sid, prefer=None):
    c = _c()
    row = c.execute("SELECT ru FROM speeches WHERE id=?", (sid,)).fetchone()
    c.close()
    if not row:
        return
    if not (row["ru"] or "").strip():
        _set(sid, status="error", error="no text to speak")
        return
    prefer = prefer or default_backend()
    if not tts_hq.available(prefer):
        _set(sid, status="ready",
             error=f"'{prefer}' audio unavailable — text is still usable")
        return
    out = os.path.join(tts_hq.HQ_DIR, f"speech-{sid}.m4a")
    try:
        _, label = tts_hq.synth_to_file(row["ru"], out, prefer=prefer)
        _set(sid, status="ready", audio_path=out, voice=label, error=None)
    except Exception as e:  # noqa: BLE001
        print(f"[speech] audio build failed for {sid}: {e}", flush=True)
        _set(sid, status="error", error=str(e)[:300])


def regen_audio(sid, prefer=None):
    _set(sid, status="audio", error=None)
    _spawn(_build_audio, sid, prefer)
    return True


# ---------------------------------------------------------------- read

def _public(r, full=False):
    try:
        payload = json.loads(r["notes"]) if r["notes"] else {}
    except (ValueError, TypeError):
        payload = {}
    has_audio = bool(r["audio_path"]) and r["status"] == "ready"
    ver = 0
    if has_audio:
        try:
            ver = int(os.path.getmtime(r["audio_path"]))
        except OSError:
            has_audio = False
    d = {
        "id": r["id"], "title": r["title"], "source": r["source"],
        "status": r["status"], "error": r["error"],
        "has_audio": has_audio, "audio_ver": ver,
        "voice": r["voice"],
        "practice_count": r["practice_count"], "last_practiced": r["last_practiced"],
        "created_at": r["created_at"],
        "words": len((r["ru"] or "").split()),
    }
    if full:
        d.update(ru=r["ru"], en=r["en"], topic=r["topic"],
                 notes=payload.get("notes", []), flags=payload.get("flags", []))
    else:
        d["snippet"] = " ".join((r["ru"] or "").split())[:110]
    return d


def recent(limit=100):
    c = _c()
    rows = c.execute("SELECT * FROM speeches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [_public(r) for r in rows]


def get(sid):
    c = _c()
    r = c.execute("SELECT * FROM speeches WHERE id=?", (sid,)).fetchone()
    c.close()
    return _public(r, full=True) if r else None


def audio_path(sid):
    c = _c()
    r = c.execute("SELECT audio_path FROM speeches WHERE id=?", (sid,)).fetchone()
    c.close()
    p = r["audio_path"] if r else None
    return p if p and os.path.exists(p) else None


def mark_practiced(sid):
    c = _c()
    c.execute("UPDATE speeches SET practice_count = practice_count + 1, "
              "last_practiced = datetime('now') WHERE id=?", (sid,))
    c.commit()
    r = c.execute("SELECT practice_count, last_practiced FROM speeches WHERE id=?",
                  (sid,)).fetchone()
    c.close()
    return dict(r) if r else None


def delete(sid):
    p = audio_path(sid)
    c = _c()
    c.execute("DELETE FROM speeches WHERE id=?", (sid,))
    c.commit()
    c.close()
    if p:
        try:
            os.remove(p)
        except OSError:
            pass
    return True
