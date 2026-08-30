"""In-app spaced repetition, scheduled with FSRS (py-fsrs).

SQLite (`srs_cards`, `srs_reviews`) is the source of truth. Anki is now optional:
a dual-write target (off by default, `app_settings.anki_dual_write`) and an
`.apkg` export. Every "make card" decision creates an srs_card here.
"""
import datetime as _dt
import hashlib as _hashlib
import json as _json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import anki           # noqa: E402
import store          # noqa: E402

NEW_PER_DAY = int(os.environ.get("RU_SRS_NEW_PER_DAY", "20"))   # default; overridable in app_settings
DAY_CUTOFF_HOUR = int(os.environ.get("RU_SRS_DAY_CUTOFF_HOUR", "4"))


def new_per_day():
    try:
        v = int(get_setting("new_per_day", NEW_PER_DAY))
        return max(0, min(999, v))
    except (TypeError, ValueError):
        return NEW_PER_DAY

RATINGS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}

_SCHED = None

# One 1-minute learning step instead of FSRS's default (1 min, 10 min): a card
# you rate "Good" as new graduates straight to a multi-day interval rather than
# reappearing ~10 min later. "Again" still brings it back in a minute.
LEARNING_STEPS = (_dt.timedelta(minutes=1),)
RELEARNING_STEPS = (_dt.timedelta(minutes=10),)

# If you finish your reviews and a learning-step card is due again "soon" (within
# this window), surface it now rather than making you wait out the timer. Lets a
# whole day's reviews be done in one sitting.
LEARNING_HORIZON = _dt.timedelta(minutes=30)


def _scheduler():
    global _SCHED
    if _SCHED is None:
        from fsrs import Scheduler
        _SCHED = Scheduler(learning_steps=LEARNING_STEPS,
                           relearning_steps=RELEARNING_STEPS)
    return _SCHED


_PREVIEW_SCHED = None


def _preview_scheduler():
    """Fuzzing off — the "Again / Good / Easy" intervals shown on the card must
    be stable between renders (real reviews still fuzz, via _scheduler())."""
    global _PREVIEW_SCHED
    if _PREVIEW_SCHED is None:
        from fsrs import Scheduler
        _PREVIEW_SCHED = Scheduler(learning_steps=LEARNING_STEPS,
                                   relearning_steps=RELEARNING_STEPS,
                                   enable_fuzzing=False)
    return _PREVIEW_SCHED


# ---------------------------------------------------------------- time helpers

def _utc():
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt):
    if dt is None:
        return None
    return dt.astimezone(_dt.timezone.utc).isoformat()


def _parse(s):
    if not s:
        return None
    return _dt.datetime.fromisoformat(s)


def _day_start_iso():
    """ISO of the most recent local DAY_CUTOFF_HOUR — the 'today' boundary."""
    now = _dt.datetime.now().astimezone()
    start = now.replace(hour=DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if now < start:
        start -= _dt.timedelta(days=1)
    return start.astimezone(_dt.timezone.utc).isoformat()


def _human_delta(due_iso, ref=None):
    due = _parse(due_iso)
    ref = ref or _utc()
    secs = (due - ref).total_seconds()
    if secs < 60:
        return "<1m"
    if secs < 3600:
        return f"{round(secs / 60)}m"
    if secs < 86400:
        return f"{round(secs / 3600)}h"
    days = secs / 86400
    if days < 30:
        return f"{round(days)}d"
    if days < 365:
        return f"{round(days / 30.4)}mo"
    return f"{days / 365:.1f}y"


# ---------------------------------------------------------------- fsrs <-> row

_FSRS_COLS = ("fsrs_state", "fsrs_step", "stability", "difficulty",
              "due", "last_review")


def _row_to_fsrs(row):
    from fsrs import Card
    return Card.from_dict({
        "card_id": row["id"],
        "state": row["fsrs_state"],
        "step": row["fsrs_step"],
        "stability": row["stability"],
        "difficulty": row["difficulty"],
        "due": row["due"],
        "last_review": row["last_review"],
    })


def _fresh_card_fields():
    from fsrs import Card
    d = Card().to_dict()
    return {
        "fsrs_state": d["state"], "fsrs_step": d["step"],
        "stability": d["stability"], "difficulty": d["difficulty"],
        "due": d["due"], "last_review": d["last_review"],
    }


# ---------------------------------------------------------------- CRUD

def _card_dict(row):
    d = dict(row)
    d["front_html"], d["bolded"] = anki.front_html(
        d["sentence"], d["span_text"], bool(d["is_phrase"]))
    d["is_new"] = d["last_review"] is None
    return d


def get_card(card_id):
    c = store.connect()
    r = c.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    c.close()
    return _card_dict(r) if r else None


def card_for_candidate(candidate_id):
    c = store.connect()
    r = c.execute("SELECT * FROM srs_cards WHERE candidate_id=?",
                  (candidate_id,)).fetchone()
    c.close()
    return _card_dict(r) if r else None


def cards_for_front_word_backfill(force=False):
    """Rows needing a `front_word` computed — all of them when force, else only
    the ones still missing it."""
    c = store.connect()
    where = "" if force else "WHERE front_word IS NULL OR front_word=''"
    rows = c.execute(
        f"SELECT id, span_text, normalized_text, is_phrase, sentence, translation, "
        f"dict_accented FROM srs_cards {where} ORDER BY id").fetchall()
    c.close()
    return [dict(r) for r in rows]


def count_missing_front_word():
    c = store.connect()
    n = c.execute("SELECT COUNT(*) n FROM srs_cards "
                  "WHERE front_word IS NULL OR front_word=''").fetchone()["n"]
    c.close()
    return n


def set_front_word(card_id, front_word):
    fw = (front_word or "").strip()
    if not fw:
        return 0
    c = store.connect()
    c.execute("UPDATE srs_cards SET front_word=? WHERE id=?", (fw, card_id))
    c.commit()
    c.close()
    return 1


def cards_for_video(video_id):
    """Every study card sourced from this video (directly or via its candidates),
    oldest first — for the per-content 'practice these cards' refresher."""
    c = store.connect()
    rows = c.execute(
        """SELECT * FROM srs_cards
           WHERE suspended=0
             AND ( video_id = :vid
                   OR candidate_id IN (SELECT id FROM candidates WHERE video_id = :vid) )
           ORDER BY created_at ASC, id ASC""",
        {"vid": video_id}).fetchall()
    c.close()
    return [_card_dict(r) for r in rows]


def _strip_stress(s):
    return (s or "").replace("́", "").replace("̀", "")


def create_card(sentence, span_text, normalized_text, is_phrase, translation,
                *, candidate_id=None, accented=None, dict_accented=None,
                front_word=None, video_id=None, timestamp=None, anki_note_id=None):
    """Idempotent on candidate_id. Returns the card dict.
    `accented` = target word stressed as it appears on the card;
    `dict_accented` = the stressed dictionary/citation form;
    `front_word` = headword for the 'word' front mode (defaults to the dict
    form for a single word, the span itself for a phrase)."""
    if candidate_id is not None:
        existing = card_for_candidate(candidate_id)
        if existing:
            return existing
    sentence = _strip_stress(sentence)          # front is read without marks
    timestamp = _snap_ts(video_id, sentence, normalized_text, timestamp)
    if not front_word:
        front_word = (span_text.strip() if is_phrase
                      else (dict_accented or "").strip()
                      or store.yo_form(store.norm(normalized_text))
                      or normalized_text.strip())
    f = _fresh_card_fields()
    c = store.connect()
    cur = c.execute(
        """INSERT INTO srs_cards
             (candidate_id, sentence, translation, span_text, normalized_text,
              is_phrase, accented, dict_accented, front_word, video_id, timestamp,
              fsrs_state, fsrs_step, stability, difficulty, due, last_review,
              anki_note_id)
           VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?,?, ?)""",
        (candidate_id, sentence, translation, span_text, store.norm(normalized_text),
         int(bool(is_phrase)), accented, dict_accented, front_word, video_id, timestamp,
         f["fsrs_state"], f["fsrs_step"], f["stability"], f["difficulty"],
         f["due"], f["last_review"], anki_note_id))
    c.commit()
    cid = cur.lastrowid
    row = c.execute("SELECT * FROM srs_cards WHERE id=?", (cid,)).fetchone()
    c.close()
    return _card_dict(row)


def _snap_ts(video_id, sentence, normalized_text, timestamp):
    """Correct a card's stored HH:MM:SS to where the word is actually spoken."""
    if not (video_id and timestamp):
        return timestamp
    approx = store._to_secs(timestamp)
    if approx is None:
        return timestamp
    try:
        snapped = store.locate_seconds(video_id, sentence, normalized_text, approx)
    except Exception:  # noqa: BLE001
        return timestamp
    if snapped is not None and abs(snapped - approx) > 0.75:
        return store.secs_to_hms(snapped)
    return timestamp


def resnap_timestamps(video_id=None):
    """Re-run the snap over every existing card (or just one video's). Returns
    (checked, moved)."""
    c = store.connect()
    q = ("SELECT id, video_id, sentence, normalized_text, timestamp FROM srs_cards "
         "WHERE video_id IS NOT NULL AND timestamp IS NOT NULL")
    args = ()
    if video_id is not None:
        q += " AND video_id=?"; args = (video_id,)
    rows = [dict(r) for r in c.execute(q, args)]
    c.close()
    moved = 0
    for r in rows:
        new = _snap_ts(r["video_id"], r["sentence"], r["normalized_text"],
                       r["timestamp"])
        if new != r["timestamp"]:
            cc = store.connect()
            cc.execute("UPDATE srs_cards SET timestamp=? WHERE id=?",
                       (new, r["id"]))
            cc.commit()
            cc.close()
            moved += 1
    return len(rows), moved


def set_anki_note(card_id, note_id):
    c = store.connect()
    c.execute("UPDATE srs_cards SET anki_note_id=? WHERE id=?", (note_id, card_id))
    c.commit()
    c.close()


def update_card(card_id, *, sentence=None, span_text=None, translation=None,
                accented=None):
    """Edit a card's content (not its schedule). Returns the updated card dict."""
    sets, args = [], []
    if sentence is not None:
        sets += ["sentence=?"]; args += [_strip_stress(sentence.strip())]
    if span_text is not None:
        sp = span_text.strip()
        sets += ["span_text=?", "normalized_text=?", "is_phrase=?"]
        args += [sp, store.lemma_key(sp), 1 if " " in sp else 0]
    if translation is not None:
        sets += ["translation=?"]; args += [translation.strip()]
    if accented is not None:
        sets += ["accented=?"]; args += [accented.strip() or None]
    if not sets:
        return get_card(card_id)
    c = store.connect()
    c.execute(f"UPDATE srs_cards SET {', '.join(sets)} WHERE id=?",
              (*args, card_id))
    c.commit()
    c.close()
    return get_card(card_id)


def set_accent_for_lemma(normalized_text, accented, force=False):
    """Write the stress-marked dictionary form onto every card of a lemma.
    Without `force`, only fills cards that don't already have one. Rows touched."""
    acc = (accented or "").strip()
    if not normalized_text or not acc:
        return 0
    c = store.connect()
    q = "UPDATE srs_cards SET dict_accented=? WHERE normalized_text=? AND is_phrase=0"
    if not force:
        q += " AND (dict_accented IS NULL OR dict_accented='')"
    n = c.execute(q, (acc, normalized_text)).rowcount
    c.commit()
    c.close()
    return n


def set_accents_for_lemma(normalized_text, surface, dict_form, force=False):
    """Write both stressed forms (surface as-on-card, dict) onto a lemma's cards.
    Without `force`, only fills columns that are still empty. Rows touched."""
    surf, df = (surface or "").strip(), (dict_form or "").strip()
    if not normalized_text or not (surf or df):
        return 0
    sets, cond = [], []
    if surf:
        sets.append(("accented", surf)); cond.append("accented")
    if df:
        sets.append(("dict_accented", df)); cond.append("dict_accented")
    q = "UPDATE srs_cards SET " + ", ".join(f"{k}=?" for k, _ in sets)
    q += " WHERE normalized_text=? AND is_phrase=0"
    if not force:
        q += " AND (" + " OR ".join(f"{k} IS NULL OR {k}=''" for k in cond) + ")"
    c = store.connect()
    n = c.execute(q, tuple(v for _, v in sets) + (normalized_text,)).rowcount
    c.commit()
    c.close()
    return n


def accent_backfill_rows():
    """One row per distinct single-word card lemma (normalized_text, span_text,
    sentence, translation) — the newest card's context for the stress call."""
    c = store.connect()
    rows = [dict(r) for r in c.execute(
        "SELECT normalized_text, span_text, sentence, translation, MAX(id) mid "
        "FROM srs_cards WHERE is_phrase=0 AND span_text NOT LIKE '% %' "
        "GROUP BY normalized_text ORDER BY mid DESC")]
    c.close()
    return rows


_MISSING_ACCENT_WHERE = (
    "is_phrase=0 AND (accented IS NULL OR accented='' "
    "OR dict_accented IS NULL OR dict_accented='') "
    "AND span_text NOT LIKE '% %'")


def count_missing_accent():
    c = store.connect()
    n = c.execute(
        f"SELECT COUNT(*) n FROM srs_cards WHERE {_MISSING_ACCENT_WHERE}"
    ).fetchone()["n"]
    c.close()
    return n


def cards_missing_accent(limit=None):
    """One row per distinct lemma missing a stress hint (span_text,
    normalized_text, sentence). Uses the newest card's sentence as LLM context."""
    c = store.connect()
    q = (f"SELECT span_text, normalized_text, sentence, MAX(id) mid FROM srs_cards "
         f"WHERE {_MISSING_ACCENT_WHERE} "
         f"GROUP BY normalized_text ORDER BY mid DESC")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in c.execute(q)]
    c.close()
    return rows


# ---------------------------------------------------------------- review

def preview(card):
    """{rating: human-interval} for all four buttons, without persisting.
    `card` is a card id or an already-fetched row/dict (avoids a re-query when
    rendering a whole queue)."""
    row = card if isinstance(card, dict) else _raw(card)
    if not row:
        return {}
    sched = _preview_scheduler()
    from fsrs import Rating
    now = _utc()
    out = {}
    for val, rating in ((1, Rating.Again), (2, Rating.Hard),
                        (3, Rating.Good), (4, Rating.Easy)):
        card, _ = sched.review_card(_row_to_fsrs(row), rating, review_datetime=now)
        out[val] = _human_delta(card.to_dict()["due"], now)
    return out


def _raw(card_id):
    c = store.connect()
    r = c.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    c.close()
    return r


def review(card_id, rating, elapsed_ms=None, at=None):
    """Grade a card (rating 1..4), reschedule, log. Returns the updated card dict.
    `at` (ISO string) backdates the review — used when flushing offline reviews."""
    from fsrs import Rating
    row = _raw(card_id)
    if not row:
        raise KeyError(card_id)
    rating = int(rating)
    now = _utc()
    if at:
        try:
            p = _parse(at)
            if p and p.tzinfo is None:
                p = p.replace(tzinfo=_dt.timezone.utc)
            if p and p <= now:
                now = p
        except ValueError:
            pass
    card, _log = _scheduler().review_card(
        _row_to_fsrs(row), Rating(rating), review_datetime=now,
        review_duration=_td(elapsed_ms))
    d = card.to_dict()
    lapsed = int(bool(row["last_review"])) if rating == 1 else 0
    c = store.connect()
    c.execute(
        """INSERT INTO srs_reviews
             (card_id, rating, prev_state, prev_step, prev_stability,
              prev_difficulty, prev_due, prev_last_review, reviewed_at, elapsed_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (card_id, rating, row["fsrs_state"], row["fsrs_step"], row["stability"],
         row["difficulty"], row["due"], row["last_review"], _iso(now), elapsed_ms))
    c.execute(
        """UPDATE srs_cards SET fsrs_state=?, fsrs_step=?, stability=?,
             difficulty=?, due=?, last_review=?, reps=reps+1, lapses=lapses+?
           WHERE id=?""",
        (d["state"], d["step"], d["stability"], d["difficulty"], d["due"],
         d["last_review"], lapsed, card_id))
    c.commit()
    out = c.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    c.close()
    return _card_dict(out)


def _td(ms):
    return _dt.timedelta(milliseconds=ms) if ms else None


def undo_last(card_id):
    """Roll a card back to its state before the most recent review. Returns the
    restored card dict, or None if there was nothing to undo."""
    c = store.connect()
    r = c.execute(
        "SELECT * FROM srs_reviews WHERE card_id=? ORDER BY id DESC LIMIT 1",
        (card_id,)).fetchone()
    if not r:
        c.close()
        return None
    was_lapse = 1 if (r["rating"] == 1 and r["prev_last_review"]) else 0
    c.execute(
        """UPDATE srs_cards SET fsrs_state=?, fsrs_step=?, stability=?,
             difficulty=?, due=?, last_review=?,
             reps=MAX(0, reps-1), lapses=MAX(0, lapses-?)
           WHERE id=?""",
        (r["prev_state"], r["prev_step"], r["prev_stability"], r["prev_difficulty"],
         r["prev_due"], r["prev_last_review"], was_lapse, card_id))
    c.execute("DELETE FROM srs_reviews WHERE id=?", (r["id"],))
    c.commit()
    out = c.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    c.close()
    return _card_dict(out)


def suspend(card_id, on=True):
    c = store.connect()
    c.execute("UPDATE srs_cards SET suspended=? WHERE id=?",
              (1 if on else 0, card_id))
    c.commit()
    c.close()


def delete_card(card_id):
    c = store.connect()
    c.execute("DELETE FROM srs_reviews WHERE card_id=?", (card_id,))
    n = c.execute("DELETE FROM srs_cards WHERE id=?", (card_id,)).rowcount
    c.commit()
    c.close()
    return n


def delete_cards_for_candidate(candidate_id):
    c = store.connect()
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM srs_cards WHERE candidate_id=?", (candidate_id,))]
    c.close()
    for cid in ids:
        delete_card(cid)
    return len(ids)


def card_counts_by_video():
    """{video_id: n_cards} across all sources — one query, for the home list so
    each row knows whether it has a practice deck. Cards reached via a candidate
    are attributed to that candidate's video."""
    c = store.connect()
    rows = c.execute(
        """SELECT COALESCE(s.video_id,
                           (SELECT video_id FROM candidates WHERE id=s.candidate_id)) vid,
                  COUNT(*) n
           FROM srs_cards s WHERE s.suspended=0 GROUP BY vid""").fetchall()
    c.close()
    return {r["vid"]: r["n"] for r in rows if r["vid"] is not None}


def _card_ids_for_video(c, video_id):
    return [r["id"] for r in c.execute(
        """SELECT id FROM srs_cards
           WHERE video_id=? OR candidate_id IN
                 (SELECT id FROM candidates WHERE video_id=?)""",
        (video_id, video_id))]


def anki_note_ids_for_video(video_id):
    """Anki note ids for the cards sourced from this video (so the caller can
    delete the notes before the cards go)."""
    c = store.connect()
    rows = c.execute(
        """SELECT anki_note_id FROM srs_cards
           WHERE anki_note_id IS NOT NULL AND (video_id=? OR candidate_id IN
                 (SELECT id FROM candidates WHERE video_id=?))""",
        (video_id, video_id)).fetchall()
    c.close()
    return [r["anki_note_id"] for r in rows]


def delete_cards_for_video(video_id):
    c = store.connect()
    ids = _card_ids_for_video(c, video_id)
    c.close()
    for cid in ids:
        delete_card(cid)
    return len(ids)


def orphan_anki_note_ids():
    c = store.connect()
    rows = c.execute("SELECT anki_note_id FROM srs_cards "
                     "WHERE anki_note_id IS NOT NULL AND video_id IS NULL").fetchall()
    c.close()
    return [r["anki_note_id"] for r in rows]


def delete_orphan_cards():
    """Cards whose source video was hard-deleted before this became a soft
    delete — no jump-to-the-moment, no clip, no context to relink."""
    c = store.connect()
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM srs_cards WHERE video_id IS NULL")]
    c.close()
    for cid in ids:
        delete_card(cid)
    return len(ids)


def delete_cards_for_lemma(normalized_text):
    c = store.connect()
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM srs_cards WHERE normalized_text=?",
        (store.norm(normalized_text),))]
    c.close()
    for cid in ids:
        delete_card(cid)
    return len(ids)


# ---------------------------------------------------------------- the queue

def _shuffle_new_for_day(rows):
    """Randomise the presentation order of today's fresh cards while keeping the
    *selection* by creation order (the SQL LIMIT already did that). The order is
    a deterministic function of (today's day boundary, card id): stable across
    queue reloads within the day, and unchanged as cards drop out of the set
    once reviewed — so a session that's reloaded mid-way doesn't reshuffle."""
    seed = _day_start_iso()
    return sorted(rows, key=lambda r: _hashlib.md5(
        f"{seed}|{r['id']}".encode()).digest())


def _new_introduced_today(c):
    row = c.execute(
        """SELECT COUNT(*) n FROM (
             SELECT card_id, MIN(reviewed_at) m FROM srs_reviews GROUP BY card_id
           ) WHERE m >= ?""", (_day_start_iso(),)).fetchone()
    return row["n"]


def stats():
    c = store.connect()
    _now = _utc()
    now = _iso(_now)
    soon = _iso(_now + LEARNING_HORIZON)
    due = c.execute(
        """SELECT COUNT(*) n FROM srs_cards
           WHERE suspended=0 AND last_review IS NOT NULL
             AND ( due <= :now OR (fsrs_state IN (1,3) AND due <= :soon) )""",
        {"now": now, "soon": soon}).fetchone()["n"]
    new_total = c.execute(
        "SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND last_review IS NULL"
    ).fetchone()["n"]
    total = c.execute("SELECT COUNT(*) n FROM srs_cards").fetchone()["n"]
    reviewed_today = c.execute(
        "SELECT COUNT(*) n FROM srs_reviews WHERE reviewed_at >= ?",
        (_day_start_iso(),)).fetchone()["n"]
    new_left = max(0, new_per_day() - _new_introduced_today(c))
    nd = c.execute(
        "SELECT MIN(due) d FROM srs_cards WHERE suspended=0 AND last_review IS NOT NULL "
        "AND due > ?", (now,)).fetchone()["d"]
    orphans = c.execute(
        "SELECT COUNT(*) n FROM srs_cards WHERE video_id IS NULL").fetchone()["n"]
    # typical seconds per review, from the last 200 graded — median, so one card
    # left open for 3 minutes doesn't blow up the estimate. Clamped to a sane
    # band and defaulted to 6s before there's history.
    els = [r["elapsed_ms"] for r in c.execute(
        "SELECT elapsed_ms FROM srs_reviews WHERE elapsed_ms IS NOT NULL "
        "AND elapsed_ms > 0 ORDER BY id DESC LIMIT 200")]
    c.close()
    if els:
        els.sort()
        med = els[len(els) // 2] / 1000.0
        pace = max(1.5, min(30.0, med))
    else:
        pace = 6.0
    return {"due": due, "new": min(new_total, new_left),
            "new_total": new_total, "total": total,
            "reviewed_today": reviewed_today, "orphans": orphans,
            "review_pace_s": round(pace, 1),
            "next_due": _human_delta(nd) if nd else None}


_LIST_FILTERS = {
    "all":       ("1", []),
    "today":     ("id IN (SELECT card_id FROM srs_reviews WHERE reviewed_at >= :d)", ["d"]),
    "reviewed":  ("last_review IS NOT NULL", []),
    "new":       ("last_review IS NULL AND suspended=0", []),
    "learning":  ("last_review IS NOT NULL AND fsrs_state IN (1,3)", []),
    "young":     ("last_review IS NOT NULL AND fsrs_state=2 AND (stability IS NULL OR stability < 21)", []),
    "mature":    ("suspended=0 AND stability >= 21", []),
    "due":       ("suspended=0 AND last_review IS NOT NULL AND due <= :n", ["n"]),
    "suspended": ("suspended=1", []),
    "orphan":    ("video_id IS NULL", []),
}
_LIST_SORTS = {
    "added": "created_at DESC, id DESC", "oldest": "created_at ASC, id ASC",
    "due": "due ASC", "alpha": "normalized_text ASC",
    "reviewed": "last_review DESC", "hardest": "difficulty DESC, lapses DESC",
    "reps": "reps DESC",
}


def list_cards(filt="all", sort="added", q="", limit=1000, video=None):
    where, needs = _LIST_FILTERS.get(filt, _LIST_FILTERS["all"])
    params = {}
    if "d" in needs:
        params["d"] = _day_start_iso()
    if "n" in needs:
        params["n"] = _iso(_utc())
    clauses = [where]
    if video is not None:
        clauses.append("(video_id = :vid OR candidate_id IN "
                       "(SELECT id FROM candidates WHERE video_id = :vid))")
        params["vid"] = video
    if q:
        clauses.append("(span_text LIKE :q OR normalized_text LIKE :q OR translation LIKE :q)")
        params["q"] = f"%{q}%"
    order = _LIST_SORTS.get(sort, _LIST_SORTS["added"])
    params["lim"] = limit
    c = store.connect()
    rows = c.execute(
        f"SELECT * FROM srs_cards WHERE {' AND '.join(clauses)} "
        f"ORDER BY {order} LIMIT :lim", params).fetchall()
    total = c.execute(
        f"SELECT COUNT(*) n FROM srs_cards WHERE {' AND '.join(clauses)}",
        params).fetchone()["n"]
    c.close()
    now = _utc()
    out = []
    for r in rows:
        d = dict(r)
        due = _parse(d["due"])
        front, bolded = anki.front_html(d["sentence"] or "", d["span_text"],
                                        bool(d["is_phrase"]))
        out.append({
            "id": d["id"], "span_text": d["span_text"], "front_html": front,
            "bolded": bolded, "front_word": d["front_word"],
            "normalized_text": d["normalized_text"], "translation": d["translation"],
            "accented": d["accented"], "dict_accented": d["dict_accented"],
            "is_phrase": bool(d["is_phrase"]),
            "sentence": d["sentence"], "video_id": d["video_id"],
            "seconds": store._to_secs(d["timestamp"]) if d["timestamp"] else None,
            "is_new": d["last_review"] is None, "suspended": bool(d["suspended"]),
            "reps": d["reps"], "lapses": d["lapses"],
            "state": d["fsrs_state"], "stability": d["stability"],
            "due_in": _human_delta(d["due"], now) if d["last_review"] else None,
            "overdue": bool(d["last_review"] and due and due < now),
        })
    return {"cards": out, "total": total, "shown": len(out)}


def queue(limit=80):
    """The study order: cards genuinely due now, PLUS learning-step cards due
    within the next 30 min (so a session flows in one chunk instead of making
    you wait out a 1-minute step or come back later), then fresh cards up to the
    daily budget."""
    c = store.connect()
    now = _utc()
    soon = _iso(now + LEARNING_HORIZON)
    now_i = _iso(now)
    due = [dict(r) for r in c.execute(
        """SELECT * FROM srs_cards
           WHERE suspended=0 AND last_review IS NOT NULL
             AND ( due <= :now
                   OR (fsrs_state IN (1,3) AND due <= :soon) )
           ORDER BY due ASC LIMIT :lim""",
        {"now": now_i, "soon": soon, "lim": limit})]
    budget = max(0, new_per_day() - _new_introduced_today(c))
    fresh = []
    if budget:
        fresh = [dict(r) for r in c.execute(
            """SELECT * FROM srs_cards
               WHERE suspended=0 AND last_review IS NULL
               ORDER BY created_at ASC, id ASC LIMIT ?""", (budget,))]
        fresh = _shuffle_new_for_day(fresh)   # picked in order, shown shuffled
    c.close()
    return [_card_dict_from_plain(r) for r in (due + fresh)]


def _card_dict_from_plain(d):
    d["front_html"], d["bolded"] = anki.front_html(
        d["sentence"], d["span_text"], bool(d["is_phrase"]))
    d["is_new"] = d["last_review"] is None
    return d


def offline_bundle(days=3):
    """Every card that is due — or will come due within `days` — plus this
    session's fresh-card budget, so the phone can run a full review session with
    no connection. Each card carries its own `due` ISO + `due_now` so the client
    can keep the not-yet-due ones cached (clips and all) and only surface them
    when their time comes."""
    c = store.connect()
    now = _utc()
    horizon = _iso(now + _dt.timedelta(days=max(0, days)))
    now_i = _iso(now)
    rows = [dict(r) for r in c.execute(
        """SELECT * FROM srs_cards
           WHERE suspended=0 AND last_review IS NOT NULL AND due <= :h
           ORDER BY due ASC""", {"h": horizon})]
    budget = max(0, new_per_day() - _new_introduced_today(c))
    if budget:
        rows += _shuffle_new_for_day([dict(r) for r in c.execute(
            """SELECT * FROM srs_cards
               WHERE suspended=0 AND last_review IS NULL
               ORDER BY created_at ASC, id ASC LIMIT ?""", (budget,))])
    c.close()
    out = []
    for r in rows:
        d = _card_dict_from_plain(r)
        d["due_now"] = (d["last_review"] is None) or (d["due"] is not None
                                                      and d["due"] <= now_i)
        out.append(d)
    return {"generated_at": now_i, "days": days, "cards": out}


# ---------------------------------------------------------------- analytics

def analytics(days=30):
    c = store.connect()
    q = c.execute
    new = q("SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND last_review IS NULL").fetchone()["n"]
    learning = q("SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND last_review IS NOT NULL "
                 "AND fsrs_state IN (1,3)").fetchone()["n"]
    review = q("SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND last_review IS NOT NULL "
               "AND fsrs_state=2").fetchone()["n"]
    suspended = q("SELECT COUNT(*) n FROM srs_cards WHERE suspended=1").fetchone()["n"]
    mature = q("SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND stability >= 21").fetchone()["n"]
    total = q("SELECT COUNT(*) n FROM srs_cards").fetchone()["n"]
    total_reviews = q("SELECT COUNT(*) n FROM srs_reviews").fetchone()["n"]
    today0 = _day_start_iso()
    reviews_today = q("SELECT COUNT(*) n FROM srs_reviews WHERE reviewed_at >= ?",
                      (today0,)).fetchone()["n"]

    by_day = {r["d"]: r["n"] for r in q(
        """SELECT date(reviewed_at, 'localtime') d, COUNT(*) n
           FROM srs_reviews WHERE reviewed_at >= date('now', ?, 'localtime')
           GROUP BY d""", (f"-{days} days",))}
    days_list = []
    cur = _dt.date.today()
    for i in range(days - 1, -1, -1):
        d = (cur - _dt.timedelta(days=i)).isoformat()
        days_list.append({"date": d, "count": by_day.get(d, 0)})

    # true retention: of reviews on already-learned cards, share not rated Again
    ret = q("""SELECT COUNT(*) tot, SUM(CASE WHEN rating > 1 THEN 1 ELSE 0 END) ok
               FROM srs_reviews WHERE prev_state = 2""").fetchone()
    retention = round(ret["ok"] / ret["tot"], 3) if ret["tot"] else None

    # streak: consecutive days up to today with >=1 review
    streak = 0
    d = cur
    if not by_day.get(cur.isoformat()):
        d = cur - _dt.timedelta(days=1)       # today not done yet doesn't break it
    while by_day.get(d.isoformat()):
        streak += 1
        d -= _dt.timedelta(days=1)
    c.close()
    return {
        "counts": {"new": new, "learning": learning, "review": review,
                   "mature": mature, "suspended": suspended, "total": total},
        "reviews_today": reviews_today, "total_reviews": total_reviews,
        "retention": retention, "streak": streak,
        "reviews_by_day": days_list,
    }


# ---------------------------------------------------------------- settings

def get_setting(key, default=None):
    c = store.connect()
    r = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    c.close()
    if not r:
        return default
    try:
        return _json.loads(r["value"])
    except (ValueError, TypeError):
        return r["value"]


def set_setting(key, value):
    c = store.connect()
    c.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES(?,?)",
              (key, _json.dumps(value)))
    c.commit()
    c.close()


def anki_dual_write():
    return bool(get_setting("anki_dual_write", False))


def card_front():
    """How review cards show their front:
      'sentence' (default) — the full sentence with the target word bolded
      'word'               — just the dictionary form / common phrase form
    Reversible: the sentence is always kept and shown on the back."""
    v = get_setting("card_front", "sentence")
    return v if v in ("sentence", "word") else "sentence"


# ---------------------------------------------------------------- migration

def backfill_from_candidates(video_id=None, limit=None):
    """Create srs_cards for card_created candidates that don't have one yet.
    Fresh state (no history). Returns the count created."""
    c = store.connect()
    q = ("SELECT * FROM candidates WHERE status='card_created' "
         "AND id NOT IN (SELECT candidate_id FROM srs_cards WHERE candidate_id IS NOT NULL)")
    args = []
    if video_id is not None:
        q += " AND video_id=?"
        args.append(video_id)
    q += " ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = c.execute(q, args).fetchall()
    c.close()
    n = 0
    for r in rows:
        create_card(
            r["sentence"], r["span_text"], r["normalized_text"], r["is_phrase"],
            r["translation"], candidate_id=r["id"],
            accented=store.accent_for(store.lemma_key(r["span_text"])),
            video_id=r["video_id"], timestamp=r["timestamp_start"],
            anki_note_id=r["anki_note_id"])
        n += 1
    return n


# ---------------------------------------------------------------- .apkg export

def export_apkg(path):
    import genanki
    c = store.connect()
    rows = c.execute("SELECT * FROM srs_cards ORDER BY id").fetchall()
    c.close()
    model = genanki.Model(
        1607392319, "RU context recognition (in-app)",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[{"name": "Recognition",
                    "qfmt": '<div class="sent">{{Front}}</div>',
                    "afmt": '{{FrontSide}}<hr id="answer"><div class="tr">{{Back}}</div>'}],
        css=".card{font-size:20px;text-align:center}.sent{margin:14px}"
            ".tr{font-size:22px}b{font-weight:700}")
    deck = genanki.Deck(2059400111, "Russian::ru-anki (in-app SRS)")
    word_front = card_front() == "word"
    for r in rows:
        sent, _ = anki.front_html(r["sentence"], r["span_text"], bool(r["is_phrase"]))
        if word_front:
            front = (r["front_word"] or r["dict_accented"] or r["normalized_text"]
                     or r["span_text"] or "")
            back = f'<div class="sent">{sent}</div>'
        else:
            front, back = sent, ""
        back += (r["translation"] or "")
        acc = r["dict_accented"] if word_front else r["accented"]
        if acc:
            back += f'<div style="opacity:.55;font-size:.8em">{acc}</div>'
        deck.add_note(genanki.Note(model=model, fields=[front, back]))
    genanki.Package(deck).write_to_file(path)
    return len(rows)
