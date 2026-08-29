"""In-app spaced repetition, scheduled with FSRS (py-fsrs).

SQLite (`srs_cards`, `srs_reviews`) is the source of truth. Anki is now optional:
a dual-write target (off by default, `app_settings.anki_dual_write`) and an
`.apkg` export. Every "make card" decision creates an srs_card here.
"""
import datetime as _dt
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


def _scheduler():
    global _SCHED
    if _SCHED is None:
        from fsrs import Scheduler
        _SCHED = Scheduler()
    return _SCHED


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


def create_card(sentence, span_text, normalized_text, is_phrase, translation,
                *, candidate_id=None, accented=None, video_id=None,
                timestamp=None, anki_note_id=None):
    """Idempotent on candidate_id. Returns the card dict."""
    if candidate_id is not None:
        existing = card_for_candidate(candidate_id)
        if existing:
            return existing
    timestamp = _snap_ts(video_id, sentence, normalized_text, timestamp)
    f = _fresh_card_fields()
    c = store.connect()
    cur = c.execute(
        """INSERT INTO srs_cards
             (candidate_id, sentence, translation, span_text, normalized_text,
              is_phrase, accented, video_id, timestamp,
              fsrs_state, fsrs_step, stability, difficulty, due, last_review,
              anki_note_id)
           VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?, ?)""",
        (candidate_id, sentence, translation, span_text, store.norm(normalized_text),
         int(bool(is_phrase)), accented, video_id, timestamp,
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


def resnap_timestamps():
    """Re-run the snap over every existing card. Returns (checked, moved)."""
    c = store.connect()
    rows = [dict(r) for r in c.execute(
        "SELECT id, video_id, sentence, normalized_text, timestamp FROM srs_cards "
        "WHERE video_id IS NOT NULL AND timestamp IS NOT NULL")]
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
        sets += ["sentence=?"]; args += [sentence.strip()]
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


# ---------------------------------------------------------------- review

def preview(card_id):
    """{rating: human-interval} for all four buttons, without persisting."""
    row = _raw(card_id)
    if not row:
        return {}
    sched = _scheduler()
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

def _new_introduced_today(c):
    row = c.execute(
        """SELECT COUNT(*) n FROM (
             SELECT card_id, MIN(reviewed_at) m FROM srs_reviews GROUP BY card_id
           ) WHERE m >= ?""", (_day_start_iso(),)).fetchone()
    return row["n"]


def stats():
    c = store.connect()
    now = _iso(_utc())
    due = c.execute(
        "SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND last_review IS NOT NULL "
        "AND due <= ?", (now,)).fetchone()["n"]
    new_total = c.execute(
        "SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND last_review IS NULL"
    ).fetchone()["n"]
    total = c.execute("SELECT COUNT(*) n FROM srs_cards").fetchone()["n"]
    reviewed_today = c.execute(
        "SELECT COUNT(*) n FROM srs_reviews WHERE reviewed_at >= ?",
        (_day_start_iso(),)).fetchone()["n"]
    new_left = max(0, new_per_day() - _new_introduced_today(c))
    c.close()
    return {"due": due, "new": min(new_total, new_left),
            "new_total": new_total, "total": total,
            "reviewed_today": reviewed_today}


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
}
_LIST_SORTS = {
    "added": "created_at DESC, id DESC", "oldest": "created_at ASC, id ASC",
    "due": "due ASC", "alpha": "normalized_text ASC",
    "reviewed": "last_review DESC", "hardest": "difficulty DESC, lapses DESC",
    "reps": "reps DESC",
}


def list_cards(filt="all", sort="added", q="", limit=1000):
    where, needs = _LIST_FILTERS.get(filt, _LIST_FILTERS["all"])
    params = {}
    if "d" in needs:
        params["d"] = _day_start_iso()
    if "n" in needs:
        params["n"] = _iso(_utc())
    clauses = [where]
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
            "bolded": bolded,
            "normalized_text": d["normalized_text"], "translation": d["translation"],
            "accented": d["accented"], "is_phrase": bool(d["is_phrase"]),
            "sentence": d["sentence"], "video_id": d["video_id"],
            "seconds": store._to_secs(d["timestamp"]) if d["timestamp"] else None,
            "is_new": d["last_review"] is None, "suspended": bool(d["suspended"]),
            "reps": d["reps"], "lapses": d["lapses"],
            "state": d["fsrs_state"], "stability": d["stability"],
            "due_in": _human_delta(d["due"], now) if d["last_review"] else None,
            "overdue": bool(d["last_review"] and due and due < now),
        })
    return {"cards": out, "total": total, "shown": len(out)}


def queue(limit=60):
    """The study order: due learning/review cards (soonest first), then as many
    fresh cards as the daily new budget allows."""
    c = store.connect()
    now = _iso(_utc())
    due = [dict(r) for r in c.execute(
        """SELECT * FROM srs_cards
           WHERE suspended=0 AND last_review IS NOT NULL AND due <= ?
           ORDER BY due ASC LIMIT ?""", (now, limit))]
    budget = max(0, new_per_day() - _new_introduced_today(c))
    fresh = []
    if budget:
        fresh = [dict(r) for r in c.execute(
            """SELECT * FROM srs_cards
               WHERE suspended=0 AND last_review IS NULL
               ORDER BY created_at ASC, id ASC LIMIT ?""", (budget,))]
    c.close()
    return [_card_dict_from_plain(r) for r in (due + fresh)]


def _card_dict_from_plain(d):
    d["front_html"], d["bolded"] = anki.front_html(
        d["sentence"], d["span_text"], bool(d["is_phrase"]))
    d["is_new"] = d["last_review"] is None
    return d


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
    for r in rows:
        front, _ = anki.front_html(r["sentence"], r["span_text"], bool(r["is_phrase"]))
        back = (r["translation"] or "")
        if r["accented"]:
            back += f'<div style="opacity:.55;font-size:.8em">{r["accented"]}</div>'
        deck.add_note(genanki.Note(model=model, fields=[front, back]))
    genanki.Package(deck).write_to_file(path)
    return len(rows)
