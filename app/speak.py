"""Reformulation-based speaking practice.

The learner does a batch of prompts (say a concrete English thought in Russian),
then one LLM pass grades each attempt (one native version + a tiered diff) and a
second pass picks the 5 highest-leverage cards from the whole session. Gaps
become production cards (srs_cards.card_type='production'); every correction is
categorised so a stats view can show recurring weak spots over time.

This module is the store + orchestration; the LLM prompts live in llm.py, the
FSRS card creation in srs.create_production_card, the endpoints in main.py.
"""
import json
import re

import llm
import srs
import store

# categories we bucket corrections into, for the stats view
CATEGORIES = ("aspect", "case", "word-order", "agreement", "conjugation",
              "lexical", "preposition", "spelling", "clarity", "other")

_PUNCT_STRIP = re.compile(r"[^\w\s]", re.U)


def _bare(s):
    """lowercase, ё->е, no punctuation — for spotting punctuation-only 'fixes'."""
    return _PUNCT_STRIP.sub("", (s or "").lower().replace("ё", "е")).strip()


def _c():
    return store.connect()


# ------------------------------------------------------------------ prompts

def new_prompt(level="a2plus", model=None):
    """Generate + store one LLM prompt from a fresh random scene seed.
    -> {id, text, hint, source}."""
    obj = llm.speaking_prompt(recent=recent_prompt_texts(), level=level, model=model)
    text = (obj.get("text") or "").strip()
    if not text:
        raise llm.LLMError("empty prompt from model")
    return _insert_prompt(text, (obj.get("hint") or "").strip() or None, "llm", level)


def add_user_prompt(text, hint=None):
    return _insert_prompt(text.strip(), (hint or "").strip() or None, "user", "b2")


def _insert_prompt(text, hint, source, level="a2plus"):
    c = _c()
    cur = c.execute(
        "INSERT INTO speak_prompts(text, hint, source, level) VALUES(?,?,?,?)",
        (text, hint, source, level))
    c.commit()
    pid = cur.lastrowid
    c.close()
    return {"id": pid, "text": text, "hint": hint, "source": source}


def recent_prompt_texts(limit=12):
    c = _c()
    rows = c.execute("SELECT text FROM speak_prompts ORDER BY id DESC LIMIT ?",
                     (limit,)).fetchall()
    c.close()
    return [r["text"] for r in rows]


def get_prompt(pid):
    c = _c()
    r = c.execute("SELECT * FROM speak_prompts WHERE id=?", (pid,)).fetchone()
    c.close()
    return dict(r) if r else None


# ------------------------------------------------------------------ attempts

def start_attempt(prompt_id, user_text, input_method="typed"):
    """Record the attempt as 'grading'. Caller then runs grade_attempt() in the
    background. -> attempt id."""
    c = _c()
    cur = c.execute(
        """INSERT INTO speak_attempts(prompt_id, user_text, input_method, status)
           VALUES(?,?,?, 'grading')""",
        (prompt_id, (user_text or "").strip(), input_method))
    c.commit()
    aid = cur.lastrowid
    c.close()
    return aid


def grade_attempt(attempt_id, model=None):
    """Run the LLM feedback call and persist reformulations + corrections + diff.
    Safe to call in a background thread; sets status to 'done' / 'error'."""
    c = _c()
    row = c.execute(
        """SELECT a.id, a.user_text, p.text AS thought, p.level
           FROM speak_attempts a JOIN speak_prompts p ON p.id = a.prompt_id
           WHERE a.id=?""", (attempt_id,)).fetchone()
    c.close()
    if not row:
        return
    try:
        fb = llm.speaking_feedback(row["thought"], row["user_text"],
                                   level=row["level"], model=model)
    except Exception as e:  # noqa: BLE001
        _fail(attempt_id, str(e)[:400])
        print(f"[speak] grade {attempt_id} failed: {e}", flush=True)
        return
    _store_feedback(attempt_id, fb)


def _fail(attempt_id, msg):
    c = _c()
    c.execute("UPDATE speak_attempts SET status='error', error=? WHERE id=?",
              (msg, attempt_id))
    c.commit()
    c.close()


def reset_grading(attempt_id):
    c = _c()
    c.execute("UPDATE speak_attempts SET status='grading', error=NULL WHERE id=?",
              (attempt_id,))
    c.commit()
    c.close()


def _drop_punct_only(fb):
    """The learner types on a phone / uses STT — a 'fix' that only adds a comma
    or fixes capitalisation is noise. Remove those corrections and renumber the
    diff's 1-based references so it still reconstructs the attempt."""
    corrs = fb.get("corrections") or []
    keep, remap, n = [], {}, 0
    for i, cr in enumerate(corrs, 1):
        was, now = cr.get("original") or "", cr.get("corrected") or ""
        if was and now and _bare(was) == _bare(now):
            remap[i] = None                       # dropped — fold its text into a run
        else:
            n += 1
            remap[i] = n
            keep.append(cr)
    fb["corrections"] = keep
    new_diff, buf = [], ""
    for d in fb.get("diff") or []:
        if "c" in d:
            r = remap.get(int(d["c"]), int(d["c"]))
            if r is None:
                buf += (corrs[int(d["c"]) - 1].get("original") or "")
            else:
                if buf:
                    new_diff.append({"s": buf}); buf = ""
                new_diff.append({"c": r})
        else:
            buf += d.get("s") or ""
    if buf:
        new_diff.append({"s": buf})
    fb["diff"] = new_diff
    return fb


def _store_feedback(attempt_id, fb):
    fb = _drop_punct_only(fb)
    corrs = fb.get("corrections") or []
    diff = fb.get("diff") or []
    c = _c()
    # the attempt can be deleted while the (slow) LLM grade is in flight — bail
    # quietly instead of a FOREIGN KEY 500 on the child inserts
    if not c.execute("SELECT 1 FROM speak_attempts WHERE id=?", (attempt_id,)).fetchone():
        c.close()
        print(f"[speak] attempt {attempt_id} gone before feedback landed — skipping", flush=True)
        return
    # clear any prior grading of this attempt (re-grade)
    for t in ("speak_reformulations", "speak_corrections", "speak_diff"):
        c.execute(f"DELETE FROM {t} WHERE attempt_id=?", (attempt_id,))

    for i, cr in enumerate(corrs):
        cat = (cr.get("category") or "other").strip().lower()
        if cat not in CATEGORIES:
            cat = "other"
        sev = "style" if (cr.get("severity") or "").strip().lower() == "style" else "hard"
        tier = (cr.get("tier") or "grammar").strip().lower()
        if tier not in ("lexical", "grammar", "clarity"):
            tier = "grammar"
        # keep tier/category coherent — the model sometimes mixes them
        if tier == "clarity":
            cat = "clarity"
        elif cat == "clarity":
            cat = "lexical" if tier == "lexical" else "other"
        c.execute(
            """INSERT INTO speak_corrections
                 (attempt_id, idx, tier, category, original, corrected, explanation, severity)
               VALUES(?,?,?,?,?,?,?,?)""",
            (attempt_id, i + 1, tier, cat, (cr.get("original") or "").strip(),
             (cr.get("corrected") or "").strip(),
             (cr.get("explanation") or "").strip(), sev))

    for seq, d in enumerate(diff):
        if "c" in d:
            c.execute(
                """INSERT INTO speak_diff(attempt_id, seq, text, correction_idx)
                   VALUES(?,?,?,?)""", (attempt_id, seq, "", int(d["c"])))
        else:
            c.execute(
                "INSERT INTO speak_diff(attempt_id, seq, text) VALUES(?,?,?)",
                (attempt_id, seq, d.get("s") or ""))

    c.execute(
        """UPDATE speak_attempts SET status='done', general_note=?, meaning=?,
             native=?, native_gloss=?, error=NULL WHERE id=?""",
        ((fb.get("general") or "").strip() or None,
         "drifted" if (fb.get("meaning") or "").strip().lower() == "drifted" else "ok",
         (fb.get("native") or "").strip() or None,
         (fb.get("gloss") or "").strip() or None,
         attempt_id))
    c.commit()
    c.close()


def attempt_view(attempt_id):
    """Everything the feedback screen needs. -> dict or None."""
    c = _c()
    a = c.execute(
        """SELECT a.*, p.text AS prompt_text, p.hint AS prompt_hint
           FROM speak_attempts a JOIN speak_prompts p ON p.id = a.prompt_id
           WHERE a.id=?""", (attempt_id,)).fetchone()
    if not a:
        c.close()
        return None
    a = dict(a)
    corrs = [dict(r) for r in c.execute(
        "SELECT * FROM speak_corrections WHERE attempt_id=? ORDER BY idx",
        (attempt_id,))]
    diff = [dict(r) for r in c.execute(
        "SELECT * FROM speak_diff WHERE attempt_id=? ORDER BY seq", (attempt_id,))]
    c.close()

    by_idx = {cr["idx"]: cr for cr in corrs}
    diff_out, rebuilt = [], []
    for d in diff:
        if d["correction_idx"] and d["correction_idx"] in by_idx:
            cr = by_idx[d["correction_idx"]]
            rebuilt.append(cr["original"])
            diff_out.append({"kind": "fix", "tier": cr["tier"], "category": cr["category"],
                             "was": cr["original"], "now": cr["corrected"],
                             "explanation": cr["explanation"], "severity": cr["severity"],
                             "corr_id": cr["id"]})
        else:
            rebuilt.append(d["text"])
            diff_out.append({"kind": "same", "text": d["text"]})
    # did the diff actually reproduce the attempt? if not the frontend falls back
    diff_ok = "".join(rebuilt).split() == (a["user_text"] or "").split()

    return {
        "id": a["id"], "prompt_id": a["prompt_id"],
        "prompt": a["prompt_text"], "hint": a["prompt_hint"],
        "attempt": a["user_text"], "input_method": a["input_method"],
        "status": a["status"], "error": a["error"],
        "meaning": a["meaning"], "general": a["general_note"],
        "native": a.get("native"), "native_gloss": a.get("native_gloss"),
        "corrections": [
            {"id": cr["id"], "idx": cr["idx"], "tier": cr["tier"],
             "category": cr["category"], "was": cr["original"], "now": cr["corrected"],
             "explanation": cr["explanation"], "severity": cr["severity"]}
            for cr in corrs],
        "diff": diff_out,
        "diff_ok": diff_ok,
    }


# ------------------------------------------------------- session card suggestions

def session_suggestions(attempt_ids, model=None):
    """Look across the whole session and propose 5 cards, ~3 pre-flagged
    high-leverage. -> [{front, back, alternatives, why, leverage}] (not created).
    [] if nothing worth carding."""
    items = []
    for aid in attempt_ids or []:
        v = attempt_view(aid)
        if not v or v["status"] != "done" or not v["corrections"]:
            continue
        items.append({
            "thought": v["prompt"], "attempt": v["attempt"],
            "reformulation": v.get("native") or "",
            "corrections": [{"severity": c["severity"], "category": c["category"],
                             "was": c["was"], "now": c["now"],
                             "explanation": c["explanation"]}
                            for c in v["corrections"] if c["now"] or c["explanation"]],
        })
    if not items:
        return []
    try:
        out = llm.speaking_session_cards(items, model=model)
        raw = out.get("cards") or []
    except Exception as e:  # noqa: BLE001
        print(f"[speak] session card pick failed: {e}", flush=True)
        flat = [c for it in items for c in it["corrections"]
                if c["severity"] == "hard" and c["now"]]
        raw = [{"front": f"say this naturally: “{c['was'] or '…'}”", "back": c["now"],
                "why": c["explanation"], "leverage": "high"} for c in flat[:5]]
    cards = []
    for c in raw[:6]:
        f, b = (c.get("front") or "").strip(), (c.get("back") or "").strip()
        if not (f and b):
            continue
        alts = [a.strip() for a in (c.get("alternatives") or [])
                if a and a.strip() and a.strip() != b][:3]
        cards.append({
            "front": f, "back": b, "alternatives": alts,
            "why": (c.get("why") or "").strip(),
            "leverage": "high" if (c.get("leverage") or "").lower() == "high" else "medium",
        })
    return cards


def create_session_cards(cards):
    """cards: [{front, back}] the user confirmed. -> list of created card dicts."""
    made = []
    for c in cards or []:
        f, b = (c.get("front") or "").strip(), (c.get("back") or "").strip()
        if f and b:
            made.append(srs.create_production_card(f, b, speak_ref="session"))
    return made


# ------------------------------------------------------------------ stats

def stats(days=90):
    c = _c()
    since = f"datetime('now','-{int(days)} days')"
    total_attempts = c.execute(
        f"SELECT COUNT(*) n FROM speak_attempts WHERE status='done' "
        f"AND created_at >= {since}").fetchone()["n"]
    by_cat = c.execute(
        f"""SELECT sc.category, sc.tier,
                   SUM(CASE WHEN sc.severity='hard' THEN 1 ELSE 0 END) hard,
                   SUM(CASE WHEN sc.severity='style' THEN 1 ELSE 0 END) style,
                   COUNT(*) total
            FROM speak_corrections sc JOIN speak_attempts a ON a.id = sc.attempt_id
            WHERE a.status='done' AND a.created_at >= {since}
            GROUP BY sc.category ORDER BY total DESC""").fetchall()
    recent = c.execute(
        f"""SELECT sc.category, sc.tier, sc.severity, sc.original, sc.corrected,
                   sc.explanation, a.id AS attempt_id, a.created_at, p.text AS prompt
            FROM speak_corrections sc
            JOIN speak_attempts a ON a.id = sc.attempt_id
            JOIN speak_prompts p ON p.id = a.prompt_id
            WHERE a.status='done' AND a.created_at >= {since}
            ORDER BY sc.id DESC LIMIT 60""").fetchall()
    prod_cards = c.execute(
        "SELECT COUNT(*) n FROM srs_cards WHERE card_type='production'").fetchone()["n"]
    c.close()
    return {
        "days": days,
        "attempts": total_attempts,
        "production_cards": prod_cards,
        "by_category": [dict(r) for r in by_cat],
        "recent": [dict(r) for r in recent],
    }


def history(limit=40):
    c = _c()
    rows = c.execute(
        """SELECT a.id, a.created_at, a.status, a.meaning, a.input_method,
                  p.text AS prompt,
                  (SELECT COUNT(*) FROM speak_corrections sc WHERE sc.attempt_id=a.id) corrections
           FROM speak_attempts a JOIN speak_prompts p ON p.id = a.prompt_id
           ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]
