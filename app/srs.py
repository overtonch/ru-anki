"""In-app spaced repetition, scheduled with FSRS (py-fsrs).

SQLite (`srs_cards`, `srs_reviews`) is the source of truth. Anki is now optional:
a dual-write target (off by default, `app_settings.anki_dual_write`) and an
`.apkg` export. Every "make card" decision creates an srs_card here.
"""
import datetime as _dt
import hashlib as _hashlib
import json as _json
import math as _math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import anki           # noqa: E402
import store          # noqa: E402

NEW_PER_DAY = int(os.environ.get("RU_SRS_NEW_PER_DAY", "50"))   # default; overridable in app_settings
PROD_PER_DAY = int(os.environ.get("RU_SRS_PROD_PER_DAY", "20"))  # of NEW_PER_DAY, aim for this many production
DAY_CUTOFF_HOUR = int(os.environ.get("RU_SRS_DAY_CUTOFF_HOUR", "4"))


def new_per_day():
    try:
        v = int(get_setting("new_per_day", NEW_PER_DAY))
        return max(0, min(999, v))
    except (TypeError, ValueError):
        return NEW_PER_DAY


def prod_per_day():
    """How many of the daily new-card budget should be production cards, when
    that many are available. Recognition backfills the rest, and vice versa."""
    try:
        v = int(get_setting("prod_per_day", PROD_PER_DAY))
        return max(0, min(new_per_day(), v))
    except (TypeError, ValueError):
        return min(new_per_day(), PROD_PER_DAY)

RATINGS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}

# A card failed this many times is a "leech" — it's not sticking and it clutters
# every day's review. Park it (suspend) so the user can rework or drop it.
LEECH_LAPSES = int(os.environ.get("RU_SRS_LEECH_LAPSES", "8"))

_SCHED = None

# One 1-minute learning step instead of FSRS's default (1 min, 10 min): a card
# you rate "Good" as new graduates straight to a multi-day interval rather than
# reappearing ~10 min later. "Again" still brings it back in a minute.
LEARNING_STEPS = (_dt.timedelta(minutes=1),)
RELEARNING_STEPS = (_dt.timedelta(minutes=10),)

# Floor on the interval when you PASS a graduated card. FSRS will hand a
# repeatedly-failed card a sub-day stability and then schedule it for tomorrow no
# matter whether you press Good or Easy — which feels broken and buries you in
# reviews. Like Anki's graduating / easy / minimum intervals: getting a card
# right always buys at least this much breathing room, and the card's stability
# is nudged up to match so the schedule stays self-consistent.
MIN_GOOD_DAYS = 2.0
MIN_EASY_DAYS = 4.0

# If you finish your reviews and a learning-step card is due again "soon" (within
# this window), surface it now rather than making you wait out the timer. Lets a
# whole day's reviews be done in one sitting.
LEARNING_HORIZON = _dt.timedelta(minutes=30)

# The daily batch surfaces a whole day's graduated reviews from the day's start,
# so cards get reviewed hours before their `due`. Reviewing early normally tells
# FSRS "almost no time passed" and stability barely grows — a card on a ~1-day
# interval would be trapped there forever. Fix: a review of a card that was due
# within today's batch window is SCORED as if it happened on the due date
# (review()/preview()). Reviewing much further ahead than the batch (via the card
# list) still counts as early, so it isn't rewarded.


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


def _day_end_iso():
    """ISO of the NEXT local DAY_CUTOFF_HOUR — the review-queue horizon. Graduated
    cards due anywhere in today's window are surfaced from the day's start, so
    reviews land in one daily batch instead of trickling in hour by hour."""
    return _iso(_dt.datetime.fromisoformat(_day_start_iso()) + _dt.timedelta(days=1))


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


def _aware(s):
    """FSRS does aware-datetime arithmetic; a naive timestamp anywhere (old data,
    a restored backup, a hand-edit, or a dict whose date was already parsed)
    would 500 the whole queue. Coerce str OR datetime to UTC-aware ISO."""
    if not s:
        return s
    if isinstance(s, _dt.datetime):
        dt = s
    else:
        try:
            dt = _dt.datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.isoformat()


def _row_to_fsrs(row):
    from fsrs import Card
    return Card.from_dict({
        "card_id": row["id"],
        "state": row["fsrs_state"],
        "step": row["fsrs_step"],
        "stability": row["stability"],
        "difficulty": row["difficulty"],
        "due": _aware(row["due"]),
        "last_review": _aware(row["last_review"]),
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


def missed_review_cards(days=3):
    """The last few days' worth of cards you didn't nail — a no-stakes re-run.
    Two sources, unioned and de-duped:
      • backlog: cards that came due on a past day (before today's cutoff) within
        the window and are still unreviewed — reviews you skipped;
      • fumbled: EVERY card you rated "Again" at least once in the window.
    The only thing held back is a card that's due right now — you'll meet that as
    a real review in the live queue, so no need to drill it here too. (This used
    to also hide anything due within ~2 days, which quietly dropped more than
    half of a heavy week's fumbles.)
    Flip-through only; the caller never posts /review for these, so working
    through them here doesn't clear the backlog or move anything on the
    timeline."""
    days = max(1, min(30, int(days)))
    day_start = _day_start_iso()
    floor = (_dt.datetime.fromisoformat(day_start)
             - _dt.timedelta(days=days)).isoformat()
    since = (_utc() - _dt.timedelta(days=days)).isoformat()
    now_i = _iso(_utc())
    c = store.connect()
    rows = c.execute(
        """SELECT * FROM srs_cards
           WHERE suspended = 0 AND last_review IS NOT NULL
             AND ( (due < :day_start AND due >= :floor)
                   OR ( due > :now
                        AND id IN (SELECT card_id FROM srs_reviews
                                   WHERE rating = 1 AND reviewed_at >= :since) ) )
           ORDER BY due ASC, id ASC""",
        {"day_start": day_start, "floor": floor, "since": since, "now": now_i}
    ).fetchall()
    c.close()
    return [_card_dict(r) for r in rows]


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


def audit_card(card_id, apply=True):
    """Structural sanity pass on one card. Fixes the mechanical inconsistencies
    in place (stray stress in the target, is_phrase vs the span, a front_word
    that drifted to the whole sentence or a wrong form, a stale normalized_text,
    a missing dictionary stress form). Returns the list of things it found.

    Content problems that need the model (target not in the sentence, no
    translation) are reported with a leading '!' so a caller can queue a
    recheck — they are NOT touched here."""
    c = store.connect()
    r = c.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    if not r:
        c.close()
        return []
    d = dict(r)
    found, sets, args = [], [], []

    sp0 = (d["span_text"] or "").strip()
    sp = _strip_stress(sp0)
    if sp0 != sp and sp:
        found.append("stress marks in the target word")
        sets.append("span_text=?")
        args.append(sp)

    is_ph = 1 if " " in sp else 0
    if int(bool(d["is_phrase"])) != is_ph and sp:
        found.append("is_phrase / phrase-vs-word mismatch")
        sets.append("is_phrase=?")
        args.append(is_ph)

    nt_want = sp if is_ph else (store.lemma_key(sp) or sp)
    if sp and _strip_stress(d["normalized_text"] or "") != nt_want:
        found.append("normalized_text didn't match the target")
        sets.append("normalized_text=?")
        args.append(nt_want)

    sent = _strip_stress((d["sentence"] or "").strip())
    dacc = (d["dict_accented"] or "").strip()
    fw = (d["front_word"] or "").strip()
    fw_bare = _strip_stress(fw).lower()
    fw_ok = bool(fw) and not (
        (sent and fw_bare == sent.lower()) or                       # became the sentence
        (not is_ph and " " in fw_bare) or                            # multi-word on a word card
        (is_ph and fw_bare != sp.lower()) or                         # phrase card: must be the phrase
        (not is_ph and fw_bare not in (sp.lower(), nt_want.lower())
         and (store.lemma_key(fw_bare) or fw_bare) != nt_want.lower()))
    if sp and not fw_ok:
        want_fw = sp if is_ph else (dacc or store.yo_form(nt_want) or nt_want)
        found.append("front_word had drifted")
        sets.append("front_word=?")
        args.append(want_fw)

    if not is_ph and not dacc and not (d["accented"] or ""):
        try:
            import accent as _accent
            marked, _u = _accent.mark_text(store.yo_form(nt_want) or nt_want)
            if "́" in marked:
                found.append("no dictionary stress form")
                sets.append("dict_accented=?")
                args.append(marked)
                # if front_word wasn't already being rewritten, upgrade the bare
                # form to the accented one so the study front shows the stress
                if "front_word=?" not in sets and (not fw or fw_bare == (
                        _strip_stress(marked).lower())):
                    sets.append("front_word=?")
                    args.append(marked)
        except Exception:  # noqa: BLE001
            pass

    if sp:
        _, bolded = anki.front_html(d["sentence"] or "", sp, bool(is_ph))
        if not bolded and (d["sentence"] or "").strip():
            found.append("! target not found in the sentence")
    if not (d["translation"] or "").strip() and d["last_review"] is not None:
        found.append("! no translation")

    if apply and sets:
        c.execute(f"UPDATE srs_cards SET {', '.join(sets)} WHERE id=?", (*args, card_id))
        c.commit()
    c.close()
    return found


def audit_recent(limit=40, apply=True):
    """Audit the most-recently created / edited cards — cheap to run on a timer
    and after every card creation."""
    c = store.connect()
    ids = [row["id"] for row in c.execute(
        "SELECT id FROM srs_cards ORDER BY id DESC LIMIT ?", (limit,))]
    c.close()
    hits = {}
    for cid in ids:
        f = audit_card(cid, apply=apply)
        if f:
            hits[cid] = f
    return hits


def create_card(sentence, span_text, normalized_text, is_phrase, translation,
                *, candidate_id=None, accented=None, dict_accented=None,
                front_word=None, video_id=None, timestamp=None, anki_note_id=None,
                source=None):
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
              anki_note_id, source)
           VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?,?, ?,?)""",
        (candidate_id, sentence, translation, span_text, store.norm(normalized_text),
         int(bool(is_phrase)), accented, dict_accented, front_word, video_id, timestamp,
         f["fsrs_state"], f["fsrs_step"], f["stability"], f["difficulty"],
         f["due"], f["last_review"], anki_note_id, source))
    c.commit()
    cid = cur.lastrowid
    row = c.execute("SELECT * FROM srs_cards WHERE id=?", (cid,)).fetchone()
    c.close()
    return _card_dict(row)


def create_production_card(front, back, *, speak_ref=None, note=None, meta=None):
    """A say-it-in-Russian card. `front` is the English cue, `back` is the Russian
    to produce; `note` (optional) is a short explanation shown under the answer;
    `meta` (optional dict) carries render extras — `given` chips, `target`
    highlight span(s), `contrast`, `skill`. Flows through the normal FSRS queue;
    the study screen renders it front-to-back (English → recall Russian aloud)."""
    front, back = (front or "").strip(), _strip_stress((back or "").strip())
    f = _fresh_card_fields()
    meta_json = _json.dumps({k: v for k, v in (meta or {}).items() if v}, ensure_ascii=False) \
        if meta else None
    c = store.connect()
    cur = c.execute(
        """INSERT INTO srs_cards
             (sentence, translation, span_text, normalized_text, is_phrase,
              front_word, alt_meanings, card_type, speak_ref, card_meta, source, learn_score,
              fsrs_state, fsrs_step, stability, difficulty, due, last_review, format_ver)
           VALUES (?,?,?,?,1, ?,?,'production',?,?, 'speak', NULL, ?,?,?,?,?,?, 2)""",
        (back, front, back, store.norm(back), front, (note or "").strip() or None,
         speak_ref, meta_json, f["fsrs_state"], f["fsrs_step"], f["stability"], f["difficulty"],
         f["due"], f["last_review"]))
    c.commit()
    row = c.execute("SELECT * FROM srs_cards WHERE id=?", (cur.lastrowid,)).fetchone()
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
                accented=None, alt_meanings=None, meaning_contextual=None):
    """Edit a card's content (not its schedule). Returns the updated card dict."""
    sets, args = [], []
    if sentence is not None:
        sets += ["sentence=?"]; args += [_strip_stress(sentence.strip())]
    if span_text is not None:
        sp = _strip_stress(span_text.strip())      # the target is read without marks
        ph = " " in sp
        nt = store.lemma_key(sp)
        # front_word (shown as the front in 'word' mode) and the stress hints are
        # derived from the target — they go stale on a span change. Reset them to
        # a sane instant value; the async re-derive (see main._learn_accent_async
        # from the edit endpoint) fills the accented forms back in.
        fw = sp if ph else (store.yo_form(nt) or nt)
        sets += ["span_text=?", "normalized_text=?", "is_phrase=?",
                 "front_word=?", "accented=NULL", "dict_accented=NULL"]
        args += [sp, nt, 1 if ph else 0, fw]
    if translation is not None:
        sets += ["translation=?"]; args += [translation.strip()]
    if accented is not None:
        sets += ["accented=?"]; args += [accented.strip() or None]
    if alt_meanings is not None:
        sets += ["alt_meanings=?"]; args += [alt_meanings.strip() or None]
    if meaning_contextual is not None:
        sets += ["meaning_contextual=?"]; args += [1 if meaning_contextual else 0]
    if not sets:
        return get_card(card_id)
    c = store.connect()
    c.execute(f"UPDATE srs_cards SET {', '.join(sets)} WHERE id=?",
              (*args, card_id))
    c.commit()
    c.close()
    return get_card(card_id)


def cards_to_reformat(limit=None, force=False):
    """Cards still on the v1 back format, most-imminent first (the ones about to
    be introduced get cleaned up before you see them). force=True re-does cards
    already on v2 as well (e.g. the criteria changed)."""
    c = store.connect()
    where = "" if force else "WHERE format_ver < 2"
    q = ("SELECT id, span_text, normalized_text, is_phrase, sentence, sentence_full, "
         "translation, front_word, dict_accented FROM srs_cards " + where +
         " ORDER BY (last_review IS NOT NULL) DESC, COALESCE(priority, learn_score, 0) DESC, id")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = c.execute(q).fetchall()
    c.close()
    return [dict(r) for r in rows]


def count_to_reformat():
    c = store.connect()
    n = c.execute("SELECT COUNT(*) n FROM srs_cards WHERE format_ver < 2").fetchone()["n"]
    c.close()
    return n


_V2_BAD_PRIMARY = re.compile(r"[/;]| or ")


def cards_failing_v2(limit=60):
    """v2 cards that don't actually meet the format spec — the big-bold
    translation is still a list / multiple senses, or the context never got
    trimmed and is a long paragraph. The daily pass re-runs these."""
    c = store.connect()
    rows = c.execute(
        "SELECT id, span_text, normalized_text, is_phrase, sentence, sentence_full, "
        "translation, front_word, dict_accented FROM srs_cards "
        "WHERE format_ver = 2 ORDER BY (last_review IS NOT NULL) DESC, COALESCE(priority, learn_score, 0) DESC").fetchall()
    c.close()
    out = []
    for r in rows:
        tr = (r["translation"] or "").strip()
        bad = (not tr
               or _V2_BAD_PRIMARY.search(tr)
               or len(tr) > 40
               or len((r["sentence"] or "")) > 160)
        if bad:
            out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def apply_reformat(card_id, primary, alt, context, contextual):
    """Write the v2 back onto a card. Keeps the original long context in
    sentence_full; only trims `sentence` when `context` is a usable clause."""
    c = store.connect()
    row = c.execute("SELECT sentence, sentence_full, translation FROM srs_cards WHERE id=?",
                    (card_id,)).fetchone()
    if not row:
        c.close()
        return False
    full = row["sentence_full"] or row["sentence"]
    ctx = (context or "").strip()
    new_sentence = ctx if ctx else row["sentence"]
    c.execute(
        """UPDATE srs_cards SET translation=?, alt_meanings=?, sentence=?,
             sentence_full=?, meaning_contextual=?, format_ver=2 WHERE id=?""",
        ((primary or row["translation"] or "").strip(), (alt or "").strip(),
         new_sentence, full, 1 if contextual else 0, card_id))
    c.commit()
    c.close()
    return True


def strip_span_stress():
    """One-off repair: some cards ended up with a stress mark IN span_text (it
    belongs only in accented / dict_accented). That makes the target unmatchable
    in its sentence, so the front doesn't bold it. Strip it + fix normalized_text
    and front_word. Returns the count fixed."""
    c = store.connect()
    rows = c.execute(
        "SELECT id, span_text, front_word FROM srs_cards "
        "WHERE span_text LIKE '%' || char(769) || '%' "
        "   OR span_text LIKE '%' || char(768) || '%'").fetchall()
    n = 0
    for r in rows:
        sp = _strip_stress(r["span_text"])
        fw = r["front_word"]
        if fw and _strip_stress(fw) == sp:      # front_word was the bare span -> keep in sync
            fw = sp
        c.execute("UPDATE srs_cards SET span_text=?, normalized_text=?, front_word=? WHERE id=?",
                  (sp, store.lemma_key(sp), fw, r["id"]))
        n += 1
    c.commit()
    c.close()
    return n


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
    # a front_word left as the bare form (e.g. right after a span edit) → the
    # freshly-computed accented dict form
    if df:
        c.execute("UPDATE srs_cards SET front_word=? WHERE normalized_text=? "
                  "AND is_phrase=0 AND (front_word IS NULL OR front_word=? OR front_word='')",
                  (df, normalized_text, normalized_text))
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

def _on_schedule_time(row, now, rating):
    """When to tell FSRS a review happened. A graduated card pulled forward by
    the daily batch (due today, before tomorrow's cutoff) but reviewed early is
    scored as if reviewed on its due date, so its stability grows normally. A
    failed review (Again), a not-yet-graduated card, or one reviewed far ahead of
    the batch window is scored at the real time."""
    if rating < 2 or row["fsrs_state"] != 2 or not row["last_review"]:
        return now
    due_dt = _parse(_aware(row["due"]))
    if not due_dt or now >= due_dt:
        return now
    eod = _parse(_aware(_day_end_iso()))
    if eod and due_dt <= eod:
        return due_dt
    return now


def _floor_pass(d, rating, scored_at):
    """Enforce MIN_GOOD/MIN_EASY on a passed graduated card. Mutates the FSRS
    to_dict `d`: bumps stability to the floor and pushes `due` out to match, so a
    card you got right is never scheduled for tomorrow. Returns d."""
    if rating < 3 or d.get("state") != 2:
        return d
    floor = MIN_EASY_DAYS if rating >= 4 else MIN_GOOD_DAYS
    lr = _parse(_aware(d.get("last_review"))) or scored_at
    due = _parse(_aware(d.get("due")))
    if due and (due - lr).total_seconds() / 86400.0 >= floor - 1e-6:
        return d                                        # FSRS already gave enough
    d["stability"] = max(d.get("stability") or 0.0, floor)
    d["due"] = _iso(lr + _dt.timedelta(days=floor))
    return d


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
        rt = _on_schedule_time(row, now, val)          # mirror review()
        card, _ = sched.review_card(_row_to_fsrs(row), rating, review_datetime=rt)
        d = _floor_pass(card.to_dict(), val, rt)
        out[val] = _human_delta(d["due"], now)
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
    # Reviewing a graduated card BEFORE it's due (the daily batch pulls a whole
    # day's reviews forward) must not be scored as "barely any time passed" —
    # that stalls stability growth. Score an early review as if it happened on
    # schedule; keep the real time for on-time / overdue reviews (which earn a
    # legitimate bonus).
    sched_at = _on_schedule_time(row, now, rating)
    card, _log = _scheduler().review_card(
        _row_to_fsrs(row), Rating(rating), review_datetime=sched_at,
        review_duration=_td(elapsed_ms))
    d = _floor_pass(card.to_dict(), rating, sched_at)
    lapsed = int(bool(row["last_review"])) if rating == 1 else 0
    # park a card that just crossed the leech threshold (never for production cards —
    # those are deliberate and few)
    new_lapses = (row["lapses"] or 0) + lapsed
    leeched = int(lapsed and not row["suspended"]
                  and (row["card_type"] or "recognition") == "recognition"
                  and new_lapses >= LEECH_LAPSES)
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
             difficulty=?, due=?, last_review=?, reps=reps+1, lapses=lapses+?,
             suspended = MAX(suspended, ?)
           WHERE id=?""",
        (d["state"], d["step"], d["stability"], d["difficulty"], d["due"],
         d["last_review"], lapsed, leeched, card_id))
    c.commit()
    out = c.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    c.close()
    res = _card_dict(out)
    res["leeched"] = bool(leeched)
    return res


def _td(ms):
    return _dt.timedelta(milliseconds=ms) if ms else None


def rebuild_schedule(card_id, apply=True):
    """Replay a card's whole review log through FSRS on an idealised schedule —
    every review scored as if it happened no earlier than the card's due date —
    and reset the card's FSRS fields to the result.

    Repairs cards flattened by the early-review bug (a daily batch that pulled
    every review hours forward, so stability never grew) or by a state wipe.
    History in `srs_reviews` is left untouched. Returns (old_stability,
    new_stability, new_interval_days) or None if there's nothing to replay."""
    from fsrs import Card, Rating
    c = store.connect()
    card = c.execute("SELECT created_at, stability, fsrs_state, suspended FROM srs_cards WHERE id=?",
                     (card_id,)).fetchone()
    revs = c.execute(
        "SELECT rating, reviewed_at, elapsed_ms FROM srs_reviews WHERE card_id=? ORDER BY id",
        (card_id,)).fetchall()
    c.close()
    if not card or not revs:
        return None
    sched = _preview_scheduler()
    created = _parse(_aware(card["created_at"])) or _utc()
    fc = Card(card_id=card_id)
    # FSRS Card() defaults `due`/`last_review` to "now"; pin to creation instead
    fc = Card.from_dict({**fc.to_dict(), "due": created.isoformat(), "last_review": None})
    when = created
    prev_rating = prev_at = None
    for rv in revs:
        real = _parse(_aware(rv["reviewed_at"])) or when
        rating = int(rv["rating"])
        # collapse a run of "Again" on one card within an hour — that's drilling
        # the card back in, not repeated failures (the first Again is the failure)
        if (rating == 1 and prev_rating == 1 and prev_at
                and (real - prev_at).total_seconds() < 3600):
            prev_at = real
            continue
        cur_due = _parse(_aware(fc.to_dict()["due"])) or when
        # never score a review as "early": use the later of (actual, scheduled)
        when = max(real, cur_due, when)
        fc, _ = sched.review_card(fc, Rating(rating), review_datetime=when,
                                  review_duration=_td(rv["elapsed_ms"]))
        prev_rating, prev_at = rating, real
    # floor only the FINAL state (if the last review was a pass) — the honest
    # trajectory is otherwise left intact
    d = _floor_pass(fc.to_dict(), int(revs[-1]["rating"]), when)
    new_due = _parse(_aware(d["due"]))
    new_lr = _parse(_aware(d["last_review"]))
    iv_days = round((new_due - new_lr).total_seconds() / 86400.0, 1) if new_due and new_lr else None
    if apply:
        c = store.connect()
        c.execute(
            """UPDATE srs_cards SET fsrs_state=?, fsrs_step=?, stability=?,
                 difficulty=?, due=?, last_review=? WHERE id=?""",
            (d["state"], d["step"], d["stability"], d["difficulty"],
             d["due"], d["last_review"], card_id))
        c.commit()
        c.close()
    return (card["stability"], d["stability"], iv_days)


def _projected_due(c, day_iso):
    """How many graduated reviews will come due in the 24h window starting at
    `day_iso` — cards already reviewed inside that window don't count again."""
    start = _parse(_aware(day_iso))
    end = _iso(start + _dt.timedelta(days=1))
    return c.execute(
        "SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND last_review IS NOT NULL "
        "AND fsrs_state = 2 AND due < ? AND due >= ? AND last_review < ?",
        (end, day_iso, day_iso)).fetchone()["n"]


def daily_healthcheck(apply=True):
    """Once-a-day sanity pass over the schedule, run when the next day's cards are
    decided. Catches cards whose FSRS state was clobbered or flattened, bad
    timestamps, and a projected review spike — and repairs what it safely can so
    the learner doesn't wake up to a wall of reviews.

    Returns a report dict; also stored in app_settings['srs_healthcheck']."""
    c = store.connect()
    q = c.execute
    report = {"day": _day_start_iso()[:10], "checked_at": _iso(_utc()),
              "repaired": 0, "issues": [], "notes": []}
    bad_ids = set()

    # 1. state wipe — a card with review history but no FSRS state
    for r in q("""SELECT s.id, COUNT(rv.id) nrev FROM srs_cards s
                  JOIN srs_reviews rv ON rv.card_id = s.id
                  WHERE (s.last_review IS NULL OR s.stability IS NULL)
                  GROUP BY s.id"""):
        bad_ids.add(r["id"])
    if bad_ids:
        report["issues"].append(f"{len(bad_ids)} card(s) had review history but no FSRS state")

    # 2. impossible schedule — due before the last review, or a graduated card
    #    with a sub-hour stability it can't have earned across several reps
    n2 = 0
    for r in q("""SELECT id, stability, reps, lapses, due, last_review, fsrs_state
                  FROM srs_cards WHERE last_review IS NOT NULL"""):
        due = _parse(_aware(r["due"])); lr = _parse(_aware(r["last_review"]))
        flat = (r["fsrs_state"] == 2 and (r["reps"] or 0) >= 4 and (r["lapses"] or 0) == 0
                and (r["stability"] or 0) < 1.0)
        if (due and lr and due < lr) or flat:
            bad_ids.add(r["id"]); n2 += 1
    if n2:
        report["issues"].append(f"{n2} card(s) had an impossible / flattened schedule")

    # 2b. the state-reset bug fingerprint (stability 3.0 / difficulty 6.5 in the
    #     log — not FSRS values; the real lapse history was wiped)
    reset_ids = _reset_fingerprint_ids(c)
    still_wrong = [i for i in reset_ids if i not in bad_ids]
    if still_wrong:
        bad_ids.update(still_wrong)
        report["issues"].append(f"{len(still_wrong)} card(s) carry the state-reset fingerprint")

    # 3. repair the ones we can, by replaying their log on an honest schedule
    for cid in sorted(bad_ids):
        try:
            res = rebuild_schedule(cid, apply=apply)
            if res and res[1] is not None:
                report["repaired"] += 1
        except Exception as e:  # noqa: BLE001
            report["notes"].append(f"card {cid}: rebuild failed ({e})")
    if apply and still_wrong:
        _mark_reset_repaired(still_wrong)

    # 4. review-load projection for today + tomorrow vs. the recent daily rate
    done = [r["n"] for r in q(
        """SELECT COUNT(*) n FROM srs_reviews
           WHERE reviewed_at >= datetime('now','-8 days')
           GROUP BY date(reviewed_at)""")]
    typical = sorted(done)[len(done) // 2] if done else 0
    today_n = _projected_due(c, _day_start_iso())
    tom_n = _projected_due(c, _day_end_iso())
    report["projected"] = {"today": today_n, "tomorrow": tom_n, "typical_day": typical}
    if typical and max(today_n, tom_n) > max(60, typical * 2.5):
        report["issues"].append(
            f"review spike ahead: ~{max(today_n, tom_n)} due vs a typical {typical}/day"
            " — check for a bad batch of new cards or a scheduling fault")

    c.close()
    report["ok"] = not report["issues"]
    try:
        set_setting("srs_healthcheck", _json.dumps(report, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass
    return report


# A card whose review log contains a step from exactly stability 3.0 / difficulty
# 6.5 was hit by the Sept-2026 state-reset bug (those aren't FSRS values): its
# real lapse history was wiped and a later Good/Easy then over-inflated it.
def _reset_repaired_ids():
    try:
        return set(_json.loads(get_setting("srs_reset_repaired", "[]") or "[]"))
    except Exception:  # noqa: BLE001
        return set()


def _mark_reset_repaired(ids):
    if not ids:
        return
    set_setting("srs_reset_repaired",
                _json.dumps(sorted(_reset_repaired_ids() | set(int(i) for i in ids))))


def _reset_fingerprint_ids(c, include_repaired=False):
    hit = {r["card_id"] for r in c.execute(
        "SELECT DISTINCT card_id FROM srs_reviews "
        "WHERE ABS(prev_stability - 3.0) < 1e-6 AND ABS(prev_difficulty - 6.5) < 1e-6")}
    return hit if include_repaired else (hit - _reset_repaired_ids())


def rebuild_all_schedules(apply=True):
    """One-shot repair across the whole collection. Replays every reviewed card's
    log through FSRS on an honest schedule (no review scored as 'early'). For a
    card corrupted by the state-reset bug the honest value replaces the current
    one even if that means a SHORTER interval (it was inflated). For every other
    card the repair can only lengthen — a card doing fine is never shortened by
    a determinism difference in the replay."""
    c = store.connect()
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM srs_cards WHERE last_review IS NOT NULL")]
    reset_ids = _reset_fingerprint_ids(c)
    c.close()
    grown = shrunk = 0
    d_gain = 0.0
    repaired_reset = []
    for cid in ids:
        res = rebuild_schedule(cid, apply=False)
        if not res:
            continue
        old_s, new_s, _iv = res
        if not new_s:
            continue
        allow_shrink = cid in reset_ids
        if old_s is not None and new_s < old_s - 0.1 and not allow_shrink:
            continue                                  # protect a healthy card
        if abs(new_s - (old_s or 0)) < 0.1:
            if allow_shrink:
                repaired_reset.append(cid)            # already correct — mark done
            continue
        if apply:
            rebuild_schedule(cid, apply=True)
        if allow_shrink:
            repaired_reset.append(cid)
        if new_s > (old_s or 0):
            grown += 1
        else:
            shrunk += 1
        d_gain += new_s - (old_s or 0)
    if apply:
        _mark_reset_repaired(repaired_reset)
    return {"examined": len(ids), "grew": grown, "shrank": shrunk,
            "reset_bug_cards": len(reset_ids),
            "avg_stability_delta": round(d_gain / (grown + shrunk), 2) if (grown + shrunk) else 0}


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


# an orphan = a pipeline card whose video was hard-deleted. A hand-added
# ('manual') card or a speaking-drill production card also has no video_id but is
# deliberate — never an orphan.
_ORPHAN_WHERE = ("video_id IS NULL AND card_type = 'recognition' "
                 "AND (source IS NULL OR source NOT IN ('manual', 'speak'))")


def orphan_anki_note_ids():
    c = store.connect()
    rows = c.execute("SELECT anki_note_id FROM srs_cards "
                     f"WHERE anki_note_id IS NOT NULL AND {_ORPHAN_WHERE}").fetchall()
    c.close()
    return [r["anki_note_id"] for r in rows]


def delete_orphan_cards():
    """Cards whose source video was hard-deleted before this became a soft
    delete — no jump-to-the-moment, no clip, no context to relink."""
    c = store.connect()
    ids = [r["id"] for r in c.execute(
        f"SELECT id FROM srs_cards WHERE {_ORPHAN_WHERE}")]
    c.close()
    for cid in ids:
        delete_card(cid)
    return len(ids)


def delete_cards_for_lemma(normalized_text):
    """-> (n_deleted, [anki_note_id, …]) for the cards that had one."""
    c = store.connect()
    rows = c.execute(
        "SELECT id, anki_note_id FROM srs_cards WHERE normalized_text=?",
        (store.norm(normalized_text),)).fetchall()
    c.close()
    for r in rows:
        delete_card(r["id"])
    return len(rows), [r["anki_note_id"] for r in rows if r["anki_note_id"]]


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


# new cards are introduced highest-`priority` first; a card with no score yet
# sorts after scored ones (so the day's pass can place it), then newest-first —
# a card made 3 weeks ago whose context you've forgotten shouldn't outrank one
# you saved this morning.
_NEW_ORDER = ("priority IS NULL, priority DESC, created_at DESC, id DESC")

# how the sub-scores combine into the introduce-next priority. Tunable via
# app_settings['new_card_weights']. Speaking is the learner's stated #1 goal;
# recency guards against introducing a card long after its context went cold;
# raw frequency is deliberately light so it doesn't crowd out the classic-fiction
# vocabulary he cards on purpose.
_DEFAULT_WEIGHTS = {"speak": 0.30, "daily": 0.22, "recency": 0.20,
                    "fiction": 0.16, "freq": 0.12}
_RECENCY_HALFLIFE_DAYS = 12.0
_FICTION = None                       # {lemma: count} for the target book, lazy


def new_card_weights():
    try:
        raw = get_setting("new_card_weights", "")
        w = _json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        w = {}
    return {k: float(w.get(k, v)) for k, v in _DEFAULT_WEIGHTS.items()}


def set_new_card_weights(w):
    keep = {k: max(0.0, float(v)) for k, v in (w or {}).items() if k in _DEFAULT_WEIGHTS}
    merged = {**_DEFAULT_WEIGHTS, **keep}
    tot = sum(merged.values()) or 1.0
    merged = {k: round(v / tot, 3) for k, v in merged.items()}
    set_setting("new_card_weights", _json.dumps(merged))
    return merged


def _freq_score(lemma):
    """0-100 from the word's overall frequency rank (rank 1 ≈ 100, rank 25k ≈ 0)."""
    if not lemma:
        return 15
    r, _ = store.family_rank(lemma)
    if not r or r <= 0:
        return 12
    return int(max(0, min(100, 100 * (1 - _math.log(r) / _math.log(25000)))))


def _fiction_score(lemma):
    """0-100 from how often the lemma appears in the target classic novel."""
    global _FICTION
    if _FICTION is None:
        try:
            import books
            _FICTION = books.freq("anna_karenina")
        except Exception:  # noqa: BLE001
            _FICTION = {}
    n = _FICTION.get(lemma or "", 0)
    if n <= 0:
        return 0
    return int(max(0, min(100, 22 * _math.log(n + 1))))      # ~1→15, ~10→53, ~50→86


def _recency_score(created_at):
    dt = _parse(_aware(created_at)) if created_at else None
    if not dt:
        return 50
    days = max(0.0, (_utc() - dt).total_seconds() / 86400.0)
    return int(round(100 * _math.exp(-days / _RECENCY_HALFLIFE_DAYS)))


def cards_for_learn_ranking():
    """Every not-yet-introduced card (recognition AND production) — the pool the
    daily pass scores for introduce-next order."""
    c = store.connect()
    rows = c.execute(
        """SELECT id, span_text, normalized_text, translation, front_word, is_phrase,
                  sentence, card_type, created_at, priority
           FROM srs_cards
           WHERE suspended=0 AND last_review IS NULL
           ORDER BY id""").fetchall()
    c.close()
    return [dict(r) for r in rows]


def unranked_new_count():
    c = store.connect()
    n = c.execute("SELECT COUNT(*) n FROM srs_cards "
                  "WHERE suspended=0 AND last_review IS NULL "
                  "AND priority IS NULL").fetchone()["n"]
    c.close()
    return n


def set_learn_scores(scores):
    """Legacy single-factor score. `scores`: {card_id: 0-100}."""
    if not scores:
        return 0
    c = store.connect()
    c.executemany("UPDATE srs_cards SET learn_score=? WHERE id=?",
                  [(int(v), int(k)) for k, v in scores.items() if v is not None])
    c.commit()
    c.close()
    return len(scores)


def set_card_priorities(llm_scores):
    """`llm_scores`: {card_id: {"speak","culture","daily"} each 0-100}. Blends in
    frequency / classic-fiction / recency (computed here) and writes `priority` +
    the `priority_meta` breakdown. Cards not in `llm_scores` are left alone."""
    if not llm_scores:
        return 0
    w = new_card_weights()
    c = store.connect()
    rows = c.execute(
        "SELECT id, normalized_text, is_phrase, created_at FROM srs_cards "
        f"WHERE id IN ({','.join('?' * len(llm_scores))})",
        list(llm_scores)).fetchall()
    updates = []
    for r in rows:
        s = llm_scores.get(r["id"]) or {}
        lem = store.norm(r["normalized_text"] or "") if not r["is_phrase"] else None
        meta = {
            "speak": int(s.get("speak", 50)),
            "daily": int(s.get("daily", 50)),
            "fiction": max(int(s.get("culture", 0)), _fiction_score(lem)),
            "freq": _freq_score(lem),
            "recency": _recency_score(r["created_at"]),
        }
        pri = round(sum(w[k] * meta[k] for k in w), 2)
        meta["weights"] = w
        updates.append((pri, _json.dumps(meta, ensure_ascii=False), r["id"]))
    c.executemany("UPDATE srs_cards SET priority=?, priority_meta=? WHERE id=?", updates)
    c.commit()
    c.close()
    return len(updates)


def rescore_priorities_from_meta():
    """Recompute `priority` from each card's stored sub-scores using the current
    weights — no LLM. For after a weight change."""
    w = new_card_weights()
    c = store.connect()
    rows = c.execute(
        "SELECT id, priority_meta, created_at, normalized_text, is_phrase FROM srs_cards "
        "WHERE suspended=0 AND last_review IS NULL AND priority_meta IS NOT NULL").fetchall()
    ups = []
    for r in rows:
        try:
            m = _json.loads(r["priority_meta"])
        except Exception:  # noqa: BLE001
            continue
        m["recency"] = _recency_score(r["created_at"])      # this one drifts daily
        pri = round(sum(w[k] * float(m.get(k, 50)) for k in w), 2)
        m["weights"] = w
        ups.append((pri, _json.dumps(m, ensure_ascii=False), r["id"]))
    c.executemany("UPDATE srs_cards SET priority=?, priority_meta=? WHERE id=?", ups)
    c.commit()
    c.close()
    return len(ups)


def new_triage(limit=60, worst_first=True):
    """New (un-introduced) cards with their priority breakdown, worst-priority
    first by default — the backlog to weed."""
    order = "priority ASC, created_at ASC" if worst_first else _NEW_ORDER
    c = store.connect()
    rows = c.execute(
        f"""SELECT id, front_word, span_text, normalized_text, translation, sentence,
                   is_phrase, card_type, source, created_at, priority, priority_meta
            FROM srs_cards
            WHERE suspended=0 AND last_review IS NULL AND priority IS NOT NULL
            ORDER BY {order} LIMIT ?""", (limit,)).fetchall()
    total = c.execute("SELECT COUNT(*) n FROM srs_cards "
                      "WHERE suspended=0 AND last_review IS NULL").fetchone()["n"]
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["priority_meta"] = _json.loads(d["priority_meta"]) if d["priority_meta"] else {}
        except Exception:  # noqa: BLE001
            d["priority_meta"] = {}
        out.append(d)
    return {"cards": out, "total_new": total, "per_day": new_per_day()}


def bulk_cards(ids, action):
    """`action` in {'suspend','delete','unsuspend'}. Returns count + any Anki note
    ids freed (for the caller to delete remotely)."""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return {"n": 0, "anki_note_ids": []}
    c = store.connect()
    notes = [r["anki_note_id"] for r in c.execute(
        f"SELECT anki_note_id FROM srs_cards WHERE id IN ({','.join('?' * len(ids))}) "
        "AND anki_note_id IS NOT NULL", ids)]
    c.close()
    if action == "delete":
        for i in ids:
            delete_card(i)
    elif action in ("suspend", "unsuspend"):
        c = store.connect()
        c.executemany("UPDATE srs_cards SET suspended=? WHERE id=?",
                      [(1 if action == "suspend" else 0, i) for i in ids])
        c.commit()
        c.close()
    else:
        return {"n": 0, "anki_note_ids": []}
    return {"n": len(ids), "anki_note_ids": notes if action == "delete" else []}


def _new_introduced_today(c, card_type="recognition"):
    """How many cards of `card_type` (None = any) were first reviewed today."""
    filt = "AND c.card_type = ?" if card_type else ""
    args = ([card_type] if card_type else []) + [_day_start_iso()]
    row = c.execute(
        f"""SELECT COUNT(*) n FROM (
             SELECT r.card_id, MIN(r.reviewed_at) m FROM srs_reviews r
             JOIN srs_cards c ON c.id = r.card_id {filt}
             GROUP BY r.card_id
           ) WHERE m >= ?""", args).fetchone()
    return row["n"]


def _new_plan(c):
    """(total_left, prod_budget) for right now — enforces the daily mix: aim for
    prod_per_day() production of new_per_day() total, each type backfilling the
    other when short."""
    total = new_per_day()
    prod_today = _new_introduced_today(c, "production")
    rec_today = _new_introduced_today(c, "recognition")
    total_left = max(0, total - prod_today - rec_today)
    prod_left = max(0, prod_per_day() - prod_today)
    return total_left, min(prod_left, total_left)


def _pick_new(c, total_left, prod_budget):
    """(production_new, recognition_new) card rows for the day, both in learn-first
    order, honouring the mix and letting each type fill the other's shortfall."""
    if total_left <= 0:
        return [], []
    def pool(ct):
        return [dict(r) for r in c.execute(
            f"""SELECT * FROM srs_cards
                WHERE suspended=0 AND last_review IS NULL AND card_type=?
                ORDER BY {_NEW_ORDER} LIMIT ?""", (ct, total_left))]
    prod_pool, rec_pool = pool("production"), pool("recognition")
    take_prod = min(len(prod_pool), prod_budget)
    rem = total_left - take_prod
    take_rec = min(len(rec_pool), rem)
    rem -= take_rec
    if rem > 0:                                  # recognition ran short — give it to production
        take_prod = min(len(prod_pool), take_prod + rem)
    return prod_pool[:take_prod], rec_pool[:take_rec]


def stats():
    c = store.connect()
    _now = _utc()
    now = _iso(_now)
    soon = _iso(_now + LEARNING_HORIZON)
    eod = _day_end_iso()
    day0 = _day_start_iso()
    due = c.execute(
        """SELECT COUNT(*) n FROM srs_cards
           WHERE suspended=0 AND last_review IS NOT NULL
             AND ( (fsrs_state = 2 AND due <= :eod AND last_review < :day0)
                   OR (fsrs_state IN (1,3) AND due <= :soon)
                   OR (due <= :now AND last_review < :day0) )""",
        {"now": now, "soon": soon, "eod": eod, "day0": day0}).fetchone()["n"]
    new_total = c.execute(
        "SELECT COUNT(*) n FROM srs_cards WHERE suspended=0 AND last_review IS NULL"
    ).fetchone()["n"]
    total = c.execute("SELECT COUNT(*) n FROM srs_cards").fetchone()["n"]
    reviewed_today = c.execute(
        "SELECT COUNT(*) n FROM srs_reviews WHERE reviewed_at >= ?",
        (_day_start_iso(),)).fetchone()["n"]
    new_left, _ = _new_plan(c)
    # the next batch that would be introduced (learn-first order) — its median
    # frequency rank tells you whether you're about to hit rarer words
    per_day = new_per_day() or 1
    batch = c.execute(
        f"""SELECT normalized_text, front_word, dict_accented FROM srs_cards
            WHERE suspended=0 AND last_review IS NULL
            ORDER BY {_NEW_ORDER} LIMIT ?""", (per_day,)).fetchall()
    nd = c.execute(
        "SELECT MIN(due) d FROM srs_cards WHERE suspended=0 AND last_review IS NOT NULL "
        "AND due > ?", (eod,)).fetchone()["d"]
    orphans = c.execute(
        f"SELECT COUNT(*) n FROM srs_cards WHERE {_ORPHAN_WHERE}").fetchone()["n"]
    leeches = c.execute(
        "SELECT COUNT(*) n FROM srs_cards WHERE suspended=1 AND lapses >= ?",
        (LEECH_LAPSES,)).fetchone()["n"]
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
    lems = [store.norm((b["dict_accented"] or b["front_word"]
                        or b["normalized_text"] or "").replace("́", "")) for b in batch]
    ranked = sorted(store.family_ranks(lems).values())
    batch_rank = ranked[len(ranked) // 2] if ranked else None
    return {"due": due, "new": min(new_total, new_left),
            "new_total": new_total, "total": total,
            "new_backlog": new_total,          # un-introduced cards still in reserve
            "new_per_day": new_per_day(),
            "new_runway_days": -(-new_total // per_day) if new_total else 0,   # ceil
            "next_batch_median_rank": batch_rank,
            "reviewed_today": reviewed_today, "orphans": orphans, "leeches": leeches,
            "review_pace_s": round(pace, 1), "day_end": eod,
            "next_due": _human_delta(nd) if nd else None,
            "healthcheck": _last_healthcheck()}


def _last_healthcheck():
    try:
        raw = get_setting("srs_healthcheck", "")
        return _json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


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
    "orphan":    (_ORPHAN_WHERE, []),
    "manual":    ("source = 'manual'", []),
    "production": ("card_type = 'production'", []),
}
_LIST_SORTS = {
    "added": "created_at DESC, id DESC", "oldest": "created_at ASC, id ASC",
    "due": "due ASC", "alpha": "normalized_text ASC",
    "reviewed": "last_review DESC", "hardest": "difficulty DESC, lapses DESC",
    "reps": "reps DESC",
    "learn": "priority IS NULL, priority DESC, created_at DESC",   # introduce-first
    "priority_low": "priority ASC, created_at ASC",                # backlog to weed
}


def list_cards(filt="all", sort="added", q="", limit=1000, video=None):
    where, needs = _LIST_FILTERS.get(filt, _LIST_FILTERS["all"])
    params = {}
    if "d" in needs:
        params["d"] = _day_start_iso()
    if "n" in needs:
        params["n"] = _day_end_iso()          # "due" list = everything due today
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
            "card_type": d.get("card_type") or "recognition",
            "learn_score": d["learn_score"], "priority": d["priority"],
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
    """The study order: today's whole batch of graduated reviews (surfaced from
    the day's start), PLUS learning-step cards due within the next 30 min, then
    fresh cards up to the daily budget. A card already reviewed today never comes
    back the same day through here — no matter what its next `due` is."""
    c = store.connect()
    now = _utc()
    soon = _iso(now + LEARNING_HORIZON)
    eod = _day_end_iso()
    day0 = _day_start_iso()
    now_i = _iso(now)
    due = [dict(r) for r in c.execute(
        """SELECT * FROM srs_cards
           WHERE suspended=0 AND last_review IS NOT NULL
             AND ( (fsrs_state = 2 AND due <= :eod AND last_review < :day0)  -- today's batch, once
                   OR (fsrs_state IN (1,3) AND due <= :soon)                 -- learning steps: minute-scale
                   OR (due <= :now AND last_review < :day0) )
           ORDER BY due ASC LIMIT :lim""",
        {"now": now_i, "soon": soon, "eod": eod, "day0": day0, "lim": limit})]
    # fresh cards up to the daily budget: aim for prod_per_day() production of
    # new_per_day() total, each type backfilling the other when it runs short.
    total_left, prod_budget = _new_plan(c)
    prod, fresh = _pick_new(c, total_left, prod_budget)
    fresh = _shuffle_new_for_day(fresh)        # selected learn-first, shown shuffled
    c.close()
    return [_card_dict_from_plain(r) for r in (due + prod + fresh)]


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
    eod = _day_end_iso()
    day0 = _day_start_iso()
    rows = [dict(r) for r in c.execute(
        """SELECT * FROM srs_cards
           WHERE suspended=0 AND last_review IS NOT NULL AND due <= :h
           ORDER BY due ASC""", {"h": horizon})]
    total_left, prod_budget = _new_plan(c)
    prod_new, rec_new = _pick_new(c, total_left, prod_budget)
    rows += prod_new + _shuffle_new_for_day(rec_new)
    c.close()
    out = []
    for r in rows:
        d = _card_dict_from_plain(r)
        # "due now" = new, a learning-step card that's come up, or a graduated
        # card due today that hasn't been reviewed yet today (one daily batch)
        done_today = d["last_review"] is not None and d["last_review"] >= day0
        d["due_now"] = (d["last_review"] is None) or (not done_today and d["due"] is not None and (
            d["due"] <= eod if d.get("fsrs_state") == 2 else d["due"] <= now_i))
        out.append(d)
    return {"generated_at": now_i, "days": days, "day_end": eod, "cards": out}


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
        "word_states": store.word_state_counts(),   # {'learned': n, 'known': n, 'has_card': n}
        "reviews_today": reviews_today, "total_reviews": total_reviews,
        "retention": retention, "streak": streak,
        "reviews_by_day": days_list,
        "healthcheck": _last_healthcheck(),
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
