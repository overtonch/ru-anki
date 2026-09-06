"""Conversation partner — a spoken, turn-based Russian conversation with an LLM
in character (your girlfriend's mum at dinner, her uncle asking about your job, a
cashier, …).

The learner answers out loud in a Russian/English mix; Whisper transcribes it
(language auto-detect, so it copes with code-switching), the partner replies in
natural spoken Russian (ElevenLabs TTS), and every turn the model silently logs
the learner's Russian errors and the things they reached for English on. "End &
review" turns all of that into a debrief + draft cards.

Store + orchestration here; prompts in llm.convo_*.
"""
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import llm         # noqa: E402
import store       # noqa: E402
import tts_hq      # noqa: E402
import whisper_rt  # noqa: E402
import ytdlp       # noqa: E402

AUDIO_DIR = os.path.join(ytdlp.MEDIA_DIR, "convo-audio")


def _c():
    return store.connect()


SCENARIOS = [
    {"id": "dinner-mum", "label": "Sunday dinner with her mum",
     "brief": "You're at Sunday dinner at your girlfriend's family's place. Her "
              "mother — warm, a bit formal at first, curious about you — makes "
              "conversation across the table: how you are, how work is, whether "
              "you're eating enough."},
    {"id": "uncle-job", "label": "Her uncle asks about your work",
     "brief": "Her uncle, a practical man in his 50s, has cornered you to ask "
              "what you actually do as a programmer, whether it pays, and "
              "whether the AI everyone talks about is going to take your job."},
    {"id": "how-you-met", "label": "“So how did you two meet?”",
     "brief": "A relative at a gathering asks, kindly and a little nosily, how "
              "you and their [daughter/niece] met and got together, and what "
              "your plans are."},
    {"id": "cashier", "label": "Sorting something out at a shop",
     "brief": "You're at a small shop or pharmacy trying to buy something / ask "
              "if they have it / sort out a problem with a purchase. The person "
              "behind the counter is brisk but not unfriendly."},
    {"id": "small-talk", "label": "Small talk with a neighbour",
     "brief": "You run into your girlfriend's family's neighbour in the "
              "stairwell. Light small talk — the weather, the building, are you "
              "visiting, how do you like it here."},
    {"id": "catch-up", "label": "A friend catches up with you",
     "brief": "A Russian friend around your age you haven't seen in a while "
              "wants to catch up — what you've been up to, how the Russian is "
              "going, plans for the weekend."},
    {"id": "doctor", "label": "Explaining you're not feeling well",
     "brief": "You need to explain to someone (a pharmacist, a relative who's a "
              "nurse) that you're not feeling well — what hurts, since when, "
              "what you've tried."},
    {"id": "toast", "label": "It's your turn to say a toast",
     "brief": "You're at a family celebration and it comes round to you to say "
              "a few words before everyone drinks. The host prompts you warmly "
              "and then reacts to whatever you manage."},
]

_BY_ID = {s["id"]: s for s in SCENARIOS}
_HISTORY_TURNS = 14


# ---------------------------------------------------------------- helpers

def _turns(c, sid, limit=None):
    q = ("SELECT role, text, meta FROM convo_turns WHERE session_id=? ORDER BY seq"
         + (f" DESC LIMIT {int(limit)}" if limit else ""))
    rows = c.execute(q, (sid,)).fetchall()
    if limit:
        rows = list(reversed(rows))
    out = []
    for r in rows:
        t = {"role": r["role"], "text": r["text"]}
        if r["meta"]:
            try:
                t["meta"] = json.loads(r["meta"])
            except (ValueError, TypeError):
                pass
        out.append(t)
    return out


def _add_turn(c, sid, role, text, translation=None, audio_path=None, meta=None):
    seq = (c.execute("SELECT COALESCE(MAX(seq),0) m FROM convo_turns WHERE session_id=?",
                     (sid,)).fetchone()["m"] or 0) + 1
    c.execute(
        "INSERT INTO convo_turns(session_id, seq, role, text, translation, audio_path, meta) "
        "VALUES(?,?,?,?,?,?,?)",
        (sid, seq, role, text, translation, audio_path,
         json.dumps(meta, ensure_ascii=False) if meta else None))
    return seq


def _synth(sid, seq, text):
    """Best-effort TTS of a partner line. Returns the file path or None."""
    if not tts_hq.available():
        return None
    os.makedirs(AUDIO_DIR, exist_ok=True)
    out = os.path.join(AUDIO_DIR, f"c{sid}-{seq}.m4a")
    try:
        tts_hq.synth_to_file(text, out)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[convo] tts {sid}/{seq}: {e}", flush=True)
        return None


def transcribe_upload(data, filename):
    """Save an uploaded recording, Whisper it (code-switch aware), clean up.
    -> (text, saved_path). The path is kept so the turn can replay it."""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    ext = os.path.splitext(filename or "")[1] or ".webm"
    raw = os.path.join(AUDIO_DIR, f"in-{int(time.time()*1000)}{ext}")
    with open(raw, "wb") as f:
        f.write(data)
    try:
        segs = whisper_rt.transcribe(raw, 0, language=None)   # auto per window
        text = " ".join(s[2].strip() for s in segs if s[2].strip()).strip()
    except Exception as e:  # noqa: BLE001
        print(f"[convo] transcribe: {e}", flush=True)
        text = ""
    return text, raw


# ---------------------------------------------------------------- sessions

def create(scenario_id=None, prompt="", level="b1"):
    sc = _BY_ID.get(scenario_id or "")
    brief = (sc["brief"] if sc else "") or (prompt or "").strip()
    c = _c()
    cur = c.execute(
        "INSERT INTO convo_sessions(scenario_id, prompt, level, status) VALUES(?,?,?, 'active')",
        (scenario_id or None, (prompt or "").strip() or None, level))
    sid = cur.lastrowid
    c.commit()
    c.close()
    try:
        o = llm.convo_open(brief, prompt, level)
    except Exception as e:  # noqa: BLE001
        _set(sid, status="error", error=str(e)[:300])
        return sid
    c = _c()
    c.execute("UPDATE convo_sessions SET persona=?, situation=?, goal=? WHERE id=?",
              ((o.get("persona") or "").strip(), (o.get("situation") or "").strip(),
               (o.get("goal") or "").strip(), sid))
    seq = _add_turn(c, sid, "partner", (o.get("opening_ru") or "").strip(),
                    translation=(o.get("opening_en") or "").strip())
    c.commit()
    c.close()
    path = _synth(sid, seq, (o.get("opening_ru") or "").strip())
    if path:
        _set_turn_audio(sid, seq, path)
    return sid


def say(sid, learner_text, audio_path=None):
    """Record the learner's turn, get the partner's reply (+ its audio), log the
    errors/gaps. -> the reply turn dict, or {'error': ...}."""
    learner_text = (learner_text or "").strip()
    c = _c()
    s = c.execute("SELECT * FROM convo_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        return {"error": "no such session"}
    history = _turns(c, sid, limit=_HISTORY_TURNS)
    c.close()
    if not learner_text:
        return {"error": "nothing heard — try again"}
    try:
        r = llm.convo_reply(s["persona"], s["situation"], history, learner_text,
                            level=s["level"])
    except Exception as e:  # noqa: BLE001
        return {"error": f"couldn't get a reply: {e}"}
    gaps = [g for g in (r.get("gaps") or []) if isinstance(g, dict) and (g.get("ru") or g.get("en"))]
    errors = [e for e in (r.get("errors") or []) if isinstance(e, dict) and e.get("right")]
    reply_ru = (r.get("reply_ru") or "").strip()

    c = _c()
    _add_turn(c, sid, "learner", learner_text, audio_path=audio_path,
              meta={"gaps": gaps, "errors": errors} if (gaps or errors) else None)
    pseq = _add_turn(c, sid, "partner", reply_ru,
                     translation=(r.get("reply_en") or "").strip())
    c.commit()
    c.close()
    path = _synth(sid, pseq, reply_ru)
    if path:
        _set_turn_audio(sid, pseq, path)
    return {"seq": pseq, "reply_ru": reply_ru, "reply_en": (r.get("reply_en") or "").strip(),
            "has_audio": bool(path), "audio_ver": int(os.path.getmtime(path)) if path else 0,
            "learner_text": learner_text, "gaps": gaps, "errors": errors,
            "drifted": bool(r.get("drifted"))}


def debrief(sid):
    c = _c()
    s = c.execute("SELECT * FROM convo_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        return {"error": "no such session"}
    history = _turns(c, sid)
    c.close()
    if s["debrief"]:
        try:
            return json.loads(s["debrief"])
        except (ValueError, TypeError):
            pass
    try:
        d = llm.convo_debrief(s["persona"], s["situation"], history, level=s["level"])
    except Exception as e:  # noqa: BLE001
        return {"error": f"couldn't build the review: {e}"}
    _set(sid, status="ended", debrief=json.dumps(d, ensure_ascii=False))
    return d


def _set(sid, **cols):
    if not cols:
        return
    c = _c()
    c.execute(f"UPDATE convo_sessions SET {','.join(k+'=?' for k in cols)} WHERE id=?",
              (*cols.values(), sid))
    c.commit()
    c.close()


def _set_turn_audio(sid, seq, path):
    c = _c()
    c.execute("UPDATE convo_turns SET audio_path=? WHERE session_id=? AND seq=?",
              (path, sid, seq))
    c.commit()
    c.close()


# ---------------------------------------------------------------- read

def _public_turn(r):
    d = {"seq": r["seq"], "role": r["role"], "text": r["text"],
         "translation": r["translation"]}
    if r["audio_path"] and os.path.exists(r["audio_path"]):
        d["has_audio"] = True
        try:
            d["audio_ver"] = int(os.path.getmtime(r["audio_path"]))
        except OSError:
            d["has_audio"] = False
    if r["meta"]:
        try:
            m = json.loads(r["meta"])
            d["gaps"] = m.get("gaps", [])
            d["errors"] = m.get("errors", [])
        except (ValueError, TypeError):
            pass
    return d


def session(sid):
    c = _c()
    s = c.execute("SELECT * FROM convo_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        return None
    turns = [_public_turn(r) for r in c.execute(
        "SELECT * FROM convo_turns WHERE session_id=? ORDER BY seq", (sid,))]
    c.close()
    deb = None
    if s["debrief"]:
        try:
            deb = json.loads(s["debrief"])
        except (ValueError, TypeError):
            pass
    return {"id": s["id"], "scenario_id": s["scenario_id"], "status": s["status"],
            "error": s["error"], "persona": s["persona"], "situation": s["situation"],
            "goal": s["goal"], "level": s["level"], "turns": turns, "debrief": deb}


def turn_audio_path(sid, seq):
    c = _c()
    r = c.execute("SELECT audio_path FROM convo_turns WHERE session_id=? AND seq=?",
                  (sid, seq)).fetchone()
    c.close()
    p = r["audio_path"] if r else None
    return p if p and os.path.exists(p) else None


def recent(limit=40):
    c = _c()
    rows = c.execute(
        """SELECT cs.*, (SELECT COUNT(*) FROM convo_turns t WHERE t.session_id=cs.id AND t.role='learner') AS n_turns
           FROM convo_sessions cs ORDER BY cs.id DESC LIMIT ?""", (limit,)).fetchall()
    c.close()
    out = []
    for r in rows:
        sc = _BY_ID.get(r["scenario_id"] or "")
        out.append({"id": r["id"], "status": r["status"],
                    "label": (sc["label"] if sc else None) or (r["goal"] or "Conversation"),
                    "persona": r["persona"], "turns": r["n_turns"],
                    "created_at": r["created_at"], "has_debrief": bool(r["debrief"])})
    return out


def delete(sid):
    c = _c()
    paths = [r["audio_path"] for r in c.execute(
        "SELECT audio_path FROM convo_turns WHERE session_id=?", (sid,)) if r["audio_path"]]
    c.execute("DELETE FROM convo_turns WHERE session_id=?", (sid,))
    c.execute("DELETE FROM convo_sessions WHERE id=?", (sid,))
    c.commit()
    c.close()
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    return True
