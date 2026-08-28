"""SQLite data layer for the pipeline server.

SQLite is the single source of truth. This module owns schema init, migration of
the pre-server DB, and every read/write the API needs. All reasoning (extraction,
translation) lives elsewhere; this file only moves rows.
"""
import html as _html
import os
import re as _re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db as _legacy  # noqa: E402  (root module: norm / lemma_key / bold)

DB = os.environ.get("VOCAB_DB", os.path.join(ROOT, "vocab.db"))

norm = _legacy.norm
lemma_key = _legacy.lemma_key
bold = _legacy.bold


def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _has_column(c, table, col):
    return any(r["name"] == col for r in c.execute(f"PRAGMA table_info({table})"))


def init_db():
    """Create/upgrade the schema. Safe to run on every startup."""
    c = connect()
    for fn in ("schema.sql", "schema_v2.sql"):
        with open(os.path.join(ROOT, fn), encoding="utf-8") as f:
            c.executescript(f.read())
    # candidates.source was added by the plan; older DBs predate it.
    if not _has_column(c, "candidates", "source"):
        c.execute("ALTER TABLE candidates ADD COLUMN source TEXT NOT NULL DEFAULT 'batch'")
    # channel / thumbnail metadata for the video picker (nullable, backfilled).
    for col in ("channel TEXT", "channel_url TEXT", "thumbnail_url TEXT",
                "duration INTEGER"):
        if not _has_column(c, "videos", col.split()[0]):
            c.execute(f"ALTER TABLE videos ADD COLUMN {col}")
    # Fold any legacy known_lexicon rows into resolved_words.
    c.execute(
        """INSERT OR IGNORE INTO resolved_words(normalized_text, reason, video_id, resolved_at)
           SELECT normalized_text, 'known', first_confirmed_video_id, confirmed_at
           FROM known_lexicon"""
    )
    c.commit()
    c.close()


# ---------------------------------------------------------------- videos

def upsert_video(url, title, subs_kind, subs_lang, raw_subs,
                 channel=None, channel_url=None, thumbnail_url=None, duration=None):
    c = connect()
    c.execute(
        """INSERT INTO videos(url, title, subs_kind, subs_lang, raw_subs,
                              channel, channel_url, thumbnail_url, duration)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(url) DO UPDATE SET
             title=excluded.title, subs_kind=excluded.subs_kind,
             subs_lang=excluded.subs_lang, raw_subs=excluded.raw_subs,
             channel=COALESCE(excluded.channel, videos.channel),
             channel_url=COALESCE(excluded.channel_url, videos.channel_url),
             thumbnail_url=COALESCE(excluded.thumbnail_url, videos.thumbnail_url),
             duration=COALESCE(excluded.duration, videos.duration),
             fetched_at=datetime('now')""",
        (url, title, subs_kind, subs_lang, raw_subs,
         channel, channel_url, thumbnail_url, duration),
    )
    vid = c.execute("SELECT id FROM videos WHERE url=?", (url,)).fetchone()["id"]
    c.commit()
    c.close()
    return vid


_YT_ID = _re.compile(r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url):
    m = _YT_ID.search(url or "")
    return m.group(1) if m else None


def _thumb(url, stored):
    # Derive from the video id: hqdefault.jpg exists for every video and has a
    # stable 4:3-ish frame. yt-dlp's stored `thumbnail` is often a maxres/webp
    # URL that 404s for smaller videos.
    vid = youtube_id(url)
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return stored


def _enrich(d):
    d["youtube_id"] = youtube_id(d.get("url"))
    d["thumbnail_url"] = _thumb(d.get("url"), d.get("thumbnail_url"))
    return d


def get_video(video_id):
    c = connect()
    r = c.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    c.close()
    return _enrich(dict(r)) if r else None


def list_videos():
    c = connect()
    rows = c.execute(
        """SELECT v.id, v.url, v.title, v.channel, v.channel_url, v.thumbnail_url,
                  v.duration, v.subs_kind, v.subs_lang, v.fetched_at,
                  (SELECT count(*) FROM subtitle_lines s WHERE s.video_id=v.id) AS lines,
                  (SELECT count(*) FROM candidates k WHERE k.video_id=v.id) AS candidates,
                  (SELECT count(*) FROM candidates k WHERE k.video_id=v.id
                     AND k.status='pending') AS pending
           FROM videos v ORDER BY v.id DESC"""
    ).fetchall()
    c.close()
    return [_enrich(dict(r)) for r in rows]


def videos_missing_meta():
    c = connect()
    rows = c.execute(
        "SELECT id, url FROM videos WHERE channel IS NULL OR channel = ''"
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def set_video_meta(video_id, channel, channel_url, thumbnail_url, duration):
    c = connect()
    c.execute(
        """UPDATE videos SET channel=?, channel_url=?, thumbnail_url=?, duration=?
           WHERE id=?""",
        (channel, channel_url, thumbnail_url, duration, video_id),
    )
    c.commit()
    c.close()


# ---------------------------------------------------------------- subtitle_lines

def replace_subtitle_lines(video_id, lines):
    """lines: iterable of (start_time, text)."""
    c = connect()
    c.execute("DELETE FROM subtitle_lines WHERE video_id=?", (video_id,))
    c.executemany(
        "INSERT INTO subtitle_lines(video_id, start_time, text) VALUES(?,?,?)",
        [(video_id, ts, t) for ts, t in lines],
    )
    c.commit()
    n = c.execute(
        "SELECT count(*) FROM subtitle_lines WHERE video_id=?", (video_id,)
    ).fetchone()[0]
    c.close()
    return n


def _fold(s):
    """Case/ё fold that preserves length, so match offsets stay valid against the
    original text (unlike norm(), which also strips)."""
    return s.lower().replace("ё", "е")


def highlight(text, q):
    """HTML-escaped `text` with every case/ё-insensitive occurrence of `q`
    wrapped in <b>."""
    ft, fq = _fold(text), _fold(q).strip()
    if not fq:
        return _html.escape(text)
    out, i = [], 0
    while True:
        j = ft.find(fq, i)
        if j < 0:
            out.append(_html.escape(text[i:]))
            break
        out.append(_html.escape(text[i:j]))
        out.append("<b>" + _html.escape(text[j:j + len(fq)]) + "</b>")
        i = j + len(fq)
    return "".join(out)


def search_lines(video_id, q, limit=40):
    """Substring match over the indexed transcript. No LLM. Case/ё-insensitive.
    Each hit carries `html`: the line with the matched span(s) bolded."""
    qn = norm(q)
    c = connect()
    rows = c.execute(
        "SELECT id, start_time, text FROM subtitle_lines WHERE video_id=? ORDER BY id",
        (video_id,),
    ).fetchall()
    c.close()
    texts = [r["text"] for r in rows]
    hits = []
    for i, r in enumerate(rows):
        if qn in norm(r["text"]):
            # de-overlapped fragments are short — show a little neighbour context
            ctx = " ".join(texts[max(0, i - 1):i + 2]).strip()
            hits.append({"id": r["id"], "start_time": r["start_time"],
                         "text": ctx, "html": highlight(ctx, q)})
            if len(hits) >= limit:
                break
    return hits


def get_subtitle_line(line_id):
    c = connect()
    r = c.execute("SELECT * FROM subtitle_lines WHERE id=?", (line_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def sentence_for(video_id, timestamp, span):
    """Rebuild a readable sentence for `span` from the indexed transcript, near
    `timestamp`. The extractor no longer echoes sentences (speed) — we stitch a
    few consecutive de-overlapped fragments around where the span occurs."""
    c = connect()
    rows = c.execute(
        "SELECT start_time, text FROM subtitle_lines WHERE video_id=? ORDER BY id",
        (video_id,),
    ).fetchall()
    c.close()
    if not rows:
        return ""
    texts = [r["text"] for r in rows]
    times = [r["start_time"] or "" for r in rows]

    span = (span or "").strip()
    stems = [s for s in (_legacy._stem(w) for w in span.split()) if len(s) >= 3]

    def has_span(t):
        tl = norm(t)
        if norm(span) and norm(span) in tl:
            return True
        toks = _re.split(r"[^а-яёa-z-]+", tl)
        return any(any(tok.startswith(s) for s in stems) for tok in toks if tok)

    # anchor near the timestamp, then hunt outward for the fragment with the span
    anchor = next((i for i, t in enumerate(times) if timestamp and t >= timestamp),
                  len(times) - 1)
    center = None
    for off in range(0, 10):
        for i in (anchor - off, anchor + off):
            if 0 <= i < len(texts) and has_span(texts[i]):
                center = i
                break
        if center is not None:
            break
    if center is None:
        center = next((i for i, t in enumerate(texts) if has_span(t)), anchor)

    window = " ".join(texts[max(0, center - 1):center + 3]).strip()
    # trim to the sentence-ish piece containing the span, if we can
    for piece in _re.split(r"(?<=[.!?])\s+", window):
        if has_span(piece):
            window = piece.strip()
            break
    return window or " ".join(texts[max(0, center):center + 2]).strip()


# ---------------------------------------------------------------- exclusions

def exclusion_reason(c, normalized):
    if c.execute("SELECT 1 FROM stoplist WHERE normalized_text=?", (normalized,)).fetchone():
        return "stoplist"
    r = c.execute(
        "SELECT reason FROM resolved_words WHERE normalized_text=?", (normalized,)
    ).fetchone()
    return r["reason"] if r else None


def card_lemmas():
    """normalized_text of every word/phrase you have an Anki card for."""
    c = connect()
    rows = c.execute(
        "SELECT normalized_text FROM resolved_words WHERE reason='has_card'"
    ).fetchall()
    c.close()
    return {r["normalized_text"] for r in rows}


def recent_discards(limit=50):
    """Surface forms the learner explicitly discarded (rejected as too easy /
    not useful), newest first — a difficulty-calibration signal for extraction."""
    c = connect()
    rows = c.execute(
        """SELECT c.span_text FROM candidates c
           JOIN resolved_words r ON r.normalized_text = c.normalized_text
           WHERE c.status = 'discarded' AND r.reason = 'known'
           ORDER BY c.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    c.close()
    seen, out = set(), []
    for r in rows:
        s = (r["span_text"] or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def resolved_words_list():
    """Surface forms of everything already decided — fed to the extractor so it
    doesn't re-propose them. Small and user-specific (the stoplist is applied
    mechanically afterwards, not sent in the prompt)."""
    c = connect()
    rows = c.execute(
        """SELECT DISTINCT c.span_text FROM candidates c
           JOIN resolved_words r ON r.normalized_text = c.normalized_text
           ORDER BY c.span_text"""
    ).fetchall()
    extra = c.execute(
        """SELECT normalized_text FROM resolved_words
           WHERE normalized_text NOT IN (SELECT normalized_text FROM candidates)"""
    ).fetchall()
    c.close()
    return [r["span_text"] for r in rows] + [r["normalized_text"] for r in extra]


# ---------------------------------------------------------------- candidates

def add_candidates(video_id, items, source="batch"):
    """items: [{span_text, is_phrase, sentence, timestamp_start, translation}].
    Applies stoplist + resolved_words filter and in-video dedup. Returns
    (added, skipped[(span, reason)])."""
    c = connect()
    added, skipped = [], []
    for it in items:
        span = (it.get("span_text") or "").strip()
        if not span:
            continue
        n = lemma_key(span)
        why = exclusion_reason(c, n)
        if why:
            skipped.append((span, why))
            continue
        if c.execute(
            "SELECT 1 FROM candidates WHERE video_id=? AND normalized_text=?",
            (video_id, n),
        ).fetchone():
            skipped.append((span, "dup-in-video"))
            continue
        # If the span (or an inflected form) can't be located in its own
        # sentence, the card would show an unbolded target — drop it now rather
        # than surface a broken candidate. Not added to resolved_words: a later
        # extraction with a cleaner sentence may still surface it.
        if "\x00" not in bold(it.get("sentence") or "", span, bool(it.get("is_phrase")), "\x00"):
            skipped.append((span, "unbolded"))
            continue
        c.execute(
            """INSERT INTO candidates(video_id, span_text, normalized_text, is_phrase,
                 sentence, timestamp_start, translation, status, source)
               VALUES(?,?,?,?,?,?,?, 'pending', ?)""",
            (video_id, span, n, int(it.get("is_phrase", 0)), it.get("sentence") or "",
             it.get("timestamp_start"), it.get("translation"), source),
        )
        added.append(span)
    c.commit()
    c.close()
    return added, skipped


def discard_unbolded(video_id=None):
    """Move any pending candidate whose target can't be bolded in its sentence
    to 'discarded'. Safety net for rows that predate the add-time check."""
    c = connect()
    q = "SELECT id, sentence, span_text, is_phrase FROM candidates WHERE status='pending'"
    args = ()
    if video_id:
        q, args = q + " AND video_id=?", (video_id,)
    dropped = []
    for r in c.execute(q, args).fetchall():
        if "\x00" not in bold(r["sentence"], r["span_text"], bool(r["is_phrase"]), "\x00"):
            c.execute("UPDATE candidates SET status='discarded' WHERE id=?", (r["id"],))
            dropped.append(r["span_text"])
    c.commit()
    c.close()
    return dropped


def list_candidates(video_id, status=None):
    c = connect()
    if status:
        rows = c.execute(
            "SELECT * FROM candidates WHERE video_id=? AND status=? ORDER BY id",
            (video_id, status),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM candidates WHERE video_id=? ORDER BY id", (video_id,)
        ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_candidate(cand_id):
    c = connect()
    r = c.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def create_candidate(video_id, span_text, is_phrase, sentence, timestamp_start,
                     translation, source="live"):
    c = connect()
    n = lemma_key(span_text.strip())
    cur = c.execute(
        """INSERT INTO candidates(video_id, span_text, normalized_text, is_phrase,
             sentence, timestamp_start, translation, status, source)
           VALUES(?,?,?,?,?,?,?, 'pending', ?)""",
        (video_id, span_text.strip(), n, int(is_phrase), sentence, timestamp_start,
         translation, source),
    )
    c.commit()
    cid = cur.lastrowid
    c.close()
    return cid


def resolve_candidate(cand_id, decision):
    """decision: 'yes' -> status card_created + resolved_words(has_card);
                 'no'  -> status discarded  + resolved_words(known).
    Returns the updated candidate row (dict)."""
    c = connect()
    row = c.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone()
    if not row:
        c.close()
        raise KeyError(cand_id)
    if decision == "yes":
        status, reason = "card_created", "has_card"
    else:
        status, reason = "discarded", "known"
    c.execute("UPDATE candidates SET status=? WHERE id=?", (status, cand_id))
    c.execute(
        """INSERT INTO resolved_words(normalized_text, reason, video_id)
           VALUES(?,?,?)
           ON CONFLICT(normalized_text) DO UPDATE SET
             reason=excluded.reason, video_id=excluded.video_id,
             resolved_at=datetime('now')""",
        (row["normalized_text"], reason, row["video_id"]),
    )
    c.commit()
    out = dict(c.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone())
    c.close()
    return out
