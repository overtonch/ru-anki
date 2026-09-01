"""SQLite data layer for the pipeline server.

SQLite is the single source of truth. This module owns schema init, migration of
the pre-server DB, and every read/write the API needs. All reasoning (extraction,
translation) lives elsewhere; this file only moves rows.
"""
import html as _html
import json as _json
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
    c = sqlite3.connect(DB, timeout=15.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")   # per-connection, must be set every time
    c.execute("PRAGMA busy_timeout=15000")
    return c


def _has_column(c, table, col):
    return any(r["name"] == col for r in c.execute(f"PRAGMA table_info({table})"))


def init_db():
    """Create/upgrade the schema. Safe to run on every startup."""
    c = connect()
    c.execute("PRAGMA journal_mode=WAL")     # persists in the db file — set once
    c.execute("PRAGMA synchronous=NORMAL")   # safe under WAL, fewer fsyncs
    for fn in ("schema.sql", "schema_v2.sql"):
        with open(os.path.join(ROOT, fn), encoding="utf-8") as f:
            c.executescript(f.read())
    # candidates.source was added by the plan; older DBs predate it.
    if not _has_column(c, "candidates", "source"):
        c.execute("ALTER TABLE candidates ADD COLUMN source TEXT NOT NULL DEFAULT 'batch'")
    if not _has_column(c, "candidates", "anki_note_id"):
        c.execute("ALTER TABLE candidates ADD COLUMN anki_note_id INTEGER")
    # srs_cards.accented holds the target word stressed AS IT APPEARS on the card;
    # dict_accented holds the stressed dictionary/citation form.
    if not _has_column(c, "srs_cards", "dict_accented"):
        c.execute("ALTER TABLE srs_cards ADD COLUMN dict_accented TEXT")
    # front_word: the word in its dictionary form (or a phrase in its common
    # form) — shown as the card front when app_settings.card_front == 'word'
    # instead of the bolded sentence. Reversible: the sentence is always kept.
    if not _has_column(c, "srs_cards", "front_word"):
        c.execute("ALTER TABLE srs_cards ADD COLUMN front_word TEXT")
    # learn_score: 0–100 (higher = a learner should meet it sooner), set by a
    # daily batched LLM pass. Decides the order new cards are introduced.
    if not _has_column(c, "srs_cards", "learn_score"):
        c.execute("ALTER TABLE srs_cards ADD COLUMN learn_score INTEGER")
    # source: where the card came from. NULL/'video'/'text' = pipeline; 'manual'
    # = hand-added for something heard outside the app (no video_id, but NOT an
    # orphan — an orphan is a pipeline card whose video was hard-deleted).
    if not _has_column(c, "srs_cards", "source"):
        c.execute("ALTER TABLE srs_cards ADD COLUMN source TEXT")
    # channel / thumbnail metadata for the video picker (nullable, backfilled).
    for col in ("channel TEXT", "channel_url TEXT", "thumbnail_url TEXT",
                "duration INTEGER",
                # downloaded media for offline watching
                "media_path TEXT", "media_bytes INTEGER", "media_quality INTEGER",
                "media_status TEXT",
                # 'video' (default) | 'text' — content that lives in the same
                # pipeline (extraction / cards / word pages) but opens in a
                # reader instead of a player
                "kind TEXT NOT NULL DEFAULT 'video'",
                # 1 = archived: gone from the home list, media files freed, but
                # the row + transcript stay so cards made from it keep their
                # jump-to-the-moment / audio-clip / "all places said" links
                "hidden INTEGER NOT NULL DEFAULT 0"):
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
    # A stored cover from another source (e.g. Apple Music album art for a song)
    # is better than the letterboxed YouTube frame — keep it.
    if stored and "ytimg.com" not in stored and "ggpht.com" not in stored:
        return stored
    # Otherwise derive from the video id: hqdefault.jpg exists for every video
    # and has a stable 4:3-ish frame. yt-dlp's stored `thumbnail` is often a
    # maxres/webp URL that 404s for smaller videos.
    vid = youtube_id(url)
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return stored


def _enrich(d):
    d["youtube_id"] = youtube_id(d.get("url"))
    d["thumbnail_url"] = _thumb(d.get("url"), d.get("thumbnail_url"))
    d["media"] = {"status": d.get("media_status"), "bytes": d.get("media_bytes"),
                  "quality": d.get("media_quality")}
    return d


# everything the app needs about a video EXCEPT the raw_subs blob, which for a
# feature-length transcript is ~100 KB — never pulled just to read the title.
_VIDEO_COLS = ("id, url, title, subs_kind, subs_lang, fetched_at, channel, "
               "channel_url, thumbnail_url, duration, media_path, media_bytes, "
               "media_quality, media_status, kind, hidden")


def get_video(video_id):
    c = connect()
    r = c.execute(f"SELECT {_VIDEO_COLS} FROM videos WHERE id=?",
                  (video_id,)).fetchone()
    c.close()
    return _enrich(dict(r)) if r else None


def video_titles():
    """{video_id: {"title","kind"}} for every video — one query, for batch card
    rendering (title + whether the source can yield a real audio clip)."""
    c = connect()
    rows = c.execute("SELECT id, title, kind FROM videos").fetchall()
    c.close()
    return {r["id"]: {"title": r["title"], "kind": r["kind"]} for r in rows}


def set_song_source(video_id, url, duration=None, thumbnail_url=None):
    """Point a song at a new audio URL (and refresh its duration / art)."""
    c = connect()
    c.execute(
        "UPDATE videos SET url=?, "
        "duration=COALESCE(?, duration), thumbnail_url=COALESCE(?, thumbnail_url) "
        "WHERE id=?", (url, duration, thumbnail_url, video_id))
    c.commit()
    c.close()


def raw_subs(video_id):
    """The full subtitle/lyric text for a video (VTT). Its own call so the common
    get_video() path stays cheap."""
    c = connect()
    r = c.execute("SELECT raw_subs FROM videos WHERE id=?", (video_id,)).fetchone()
    c.close()
    return r["raw_subs"] if r else None


def list_videos(include_hidden=False):
    c = connect()
    rows = c.execute(
        f"""SELECT v.id, v.url, v.title, v.channel, v.channel_url, v.thumbnail_url,
                  v.duration, v.subs_kind, v.subs_lang, v.fetched_at, v.kind, v.hidden,
                  v.media_status, v.media_bytes, v.media_quality,
                  (SELECT count(*) FROM subtitle_lines s WHERE s.video_id=v.id) AS lines,
                  (SELECT count(*) FROM candidates k WHERE k.video_id=v.id) AS candidates,
                  (SELECT count(*) FROM candidates k WHERE k.video_id=v.id
                     AND k.status='pending') AS pending
           FROM videos v {'' if include_hidden else 'WHERE v.hidden=0'}
           ORDER BY v.id DESC"""
    ).fetchall()
    c.close()
    return [_enrich(dict(r)) for r in rows]


def hide_video(video_id):
    """Archive a video: drop it from the home list and free its downloaded
    media, but keep the row + transcript + candidates so study cards made from
    it still jump to the moment, play their clip and list every occurrence."""
    v = get_video(video_id)
    if not v:
        return 0
    _free_media_files(video_id, v)
    c = connect()
    c.execute("UPDATE videos SET hidden=1, media_path=NULL, media_bytes=NULL, "
              "media_quality=NULL, media_status=NULL WHERE id=?", (video_id,))
    c.commit()
    c.close()
    return 1


def unhide_video(video_id):
    c = connect()
    n = c.execute("UPDATE videos SET hidden=0 WHERE id=?", (video_id,)).rowcount
    c.commit()
    c.close()
    return n


def _free_media_files(video_id, v=None):
    v = v or get_video(video_id)
    if v and v.get("media_path"):
        try:
            os.remove(v["media_path"])
        except OSError:
            pass


def delete_video(video_id):
    """Hard-delete the video, its transcript and its candidates. Callers that
    want to keep the study cards use hide_video() instead; callers that want the
    cards gone delete them (srs.delete_cards_for_video) before calling this.
    Any card still pointing here is detached so the FK deletes can proceed.
    resolved_words (your decisions) stand."""
    v = get_video(video_id)
    if v and v.get("media_path"):
        try:
            os.remove(v["media_path"])
        except OSError:
            pass
    c = connect()
    # detach any surviving study cards first so the FK deletes below can proceed
    c.execute("UPDATE srs_cards SET candidate_id=NULL, video_id=NULL, timestamp=NULL "
              "WHERE video_id=? OR candidate_id IN "
              "(SELECT id FROM candidates WHERE video_id=?)", (video_id, video_id))
    for tbl in ("candidate_sentences_cache", "lyric_notes"):
        try:
            c.execute(f"DELETE FROM {tbl} WHERE video_id=?", (video_id,))
        except sqlite3.OperationalError:
            pass
    c.execute("DELETE FROM candidates WHERE video_id=?", (video_id,))
    c.execute("DELETE FROM subtitle_lines WHERE video_id=?", (video_id,))
    c.execute("UPDATE resolved_words SET video_id=NULL WHERE video_id=?", (video_id,))
    n = c.execute("DELETE FROM videos WHERE id=?", (video_id,)).rowcount
    c.commit()
    c.close()
    _LEMMA_IDX.pop(video_id, None)
    drop_sentence_cache(video_id=video_id)
    return n


def videos_missing_meta():
    c = connect()
    rows = c.execute(
        "SELECT id, url FROM videos WHERE channel IS NULL OR channel = ''"
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def set_media(video_id, **kw):
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    c = connect()
    c.execute(f"UPDATE videos SET {cols} WHERE id=?", (*kw.values(), video_id))
    c.commit()
    c.close()


def set_raw_subs(video_id, raw_subs, kind="whisper", lang="ru"):
    c = connect()
    c.execute("UPDATE videos SET raw_subs=?, subs_kind=?, subs_lang=? WHERE id=?",
              (raw_subs, kind, lang, video_id))
    c.commit()
    c.close()


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
    _LEMMA_IDX.pop(video_id, None)          # transcript changed — drop the caches
    drop_sentence_cache(video_id=video_id)
    drop_lyric_notes(video_id)
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


_SENT_SPLIT = _re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_SPLIT = _re.compile(r"\s*[—–,;:]\s+")
_WORD_RE = _re.compile(r"[А-Яа-яЁёA-Za-z]+")
_STARTS_MID = _re.compile(r"^(и|а|но|что|чтобы|как|это|то|же|ну|вот|потому|поэтому|или|да)\b")


def _stems(span):
    return [s for s in (_legacy._stem(w) for w in (span or "").split()) if len(s) >= 3]


def _contains(text, span, stems):
    tl = norm(text)
    if norm(span) and norm(span) in tl:
        return True
    toks = _re.split(r"[^а-яёa-z-]+", tl)
    return any(any(tok.startswith(s) for s in stems) for tok in toks if tok)


def _pieces(texts, lo, hi):
    blob = _re.sub(r"\s+", " ", " ".join(texts[max(0, lo):hi])).strip()
    return [p.strip() for p in _SENT_SPLIT.split(blob) if p.strip()]


def _best_sentence(texts, center, span, stems):
    """One readable sentence around line `center` containing `span` — extend a
    fragment with its neighbour, trim a run-on to the clause around the span."""
    sents = _pieces(texts, center - 2, center + 4)
    i = next((k for k, s in enumerate(sents) if _contains(s, span, stems)), None)
    if i is None:
        return " ".join(texts[max(0, center):center + 2]).strip()
    hit = sents[i]
    if len(hit.split()) < 5:                      # fragment — glue a neighbour
        nb = sents[i + 1] if i + 1 < len(sents) else (sents[i - 1] if i else "")
        if nb:
            hit = f"{hit} {nb}" if i + 1 < len(sents) else f"{nb} {hit}"
    if len(hit.split()) > 22:                     # run-on — trim to a clause window
        cl = _CLAUSE_SPLIT.split(hit)
        j = next((k for k, c in enumerate(cl) if _contains(c, span, stems)), None)
        if j is not None:
            hit = ", ".join(cl[max(0, j - 1):j + 2]).strip(" ,")
    return hit


def context_for(video_id, timestamp, span):
    """Improved `sentence_for` — a proper sentence, not a stitched fragment."""
    c = connect()
    rows = c.execute(
        "SELECT start_time, text FROM subtitle_lines WHERE video_id=? ORDER BY id",
        (video_id,)).fetchall()
    c.close()
    if not rows:
        return ""
    texts = [r["text"] for r in rows]
    times = [r["start_time"] or "" for r in rows]
    stems = _stems(span)
    anchor = next((i for i, t in enumerate(times) if timestamp and t >= timestamp),
                  len(times) - 1)
    center = None
    for off in range(14):
        for i in (anchor - off, anchor + off):
            if 0 <= i < len(texts) and _contains(texts[i], span, stems):
                center = i
                break
        if center is not None:
            break
    if center is None:
        center = next((i for i, t in enumerate(texts) if _contains(t, span, stems)), anchor)
    return _best_sentence(texts, center, span, stems)


def _ctx_norm(s):
    return _re.sub(r"[^а-яёa-z0-9 ]", "", (s or "").lower().replace("ё", "е"))


def card_context(video_id, sentence):
    """The transcript line / paragraph immediately before and after the one this
    card's sentence came from. -> {"before": str, "after": str} (either may be
    ''). Headings ('## …') are skipped. Best-effort fuzzy match."""
    if not video_id or not (sentence or "").strip():
        return {"before": "", "after": ""}
    c = connect()
    rows = c.execute(
        "SELECT text FROM subtitle_lines WHERE video_id=? ORDER BY id",
        (video_id,)).fetchall()
    c.close()
    texts = [r["text"] for r in rows if not (r["text"] or "").startswith("## ")]
    if not texts:
        return {"before": "", "after": ""}
    key = _ctx_norm(sentence)
    kset = set(key.split())
    best_i, best_score = None, 0.0
    for i, t in enumerate(texts):
        nt = _ctx_norm(t)
        if not nt:
            continue
        if key and (key in nt or (len(nt) > 12 and nt in key)):
            best_i, best_score = i, 1.0
            break
        toks = set(nt.split())
        if not toks:
            continue
        j = len(kset & toks) / len(kset | toks)
        if j > best_score:
            best_i, best_score = i, j
    if best_i is None or best_score < 0.34:
        return {"before": "", "after": ""}
    before = texts[best_i - 1].strip() if best_i > 0 else ""
    after = texts[best_i + 1].strip() if best_i + 1 < len(texts) else ""
    return {"before": before, "after": after}


def card_contexts(cards):
    """Batch card_context() for a list of card dicts -> {card_id: {before, after}}.
    Loads each source video's lines once."""
    by_vid = {}
    for c in cards:
        if c.get("video_id") and (c.get("sentence") or "").strip():
            by_vid.setdefault(c["video_id"], []).append(c)
    out = {}
    conn = connect()
    for vid, cs in by_vid.items():
        rows = conn.execute(
            "SELECT text FROM subtitle_lines WHERE video_id=? ORDER BY id",
            (vid,)).fetchall()
        texts = [r["text"] for r in rows if not (r["text"] or "").startswith("## ")]
        if not texts:
            continue
        norm = [_ctx_norm(t) for t in texts]
        for c in cs:
            key = _ctx_norm(c["sentence"])
            kset = set(key.split())
            if not kset:
                continue
            bi, bs = None, 0.0
            for i, nt in enumerate(norm):
                if not nt:
                    continue
                if key in nt or (len(nt) > 12 and nt in key):
                    bi, bs = i, 1.0
                    break
                toks = set(nt.split())
                if toks:
                    j = len(kset & toks) / len(kset | toks)
                    if j > bs:
                        bi, bs = i, j
            if bi is None or bs < 0.34:
                continue
            out[c["id"]] = {
                "before": texts[bi - 1].strip() if bi > 0 else "",
                "after": texts[bi + 1].strip() if bi + 1 < len(texts) else "",
            }
    conn.close()
    return out


def _score_sentence(text, span, stems, rank_of):
    """Higher = better flashcard context: right length, common surrounding
    vocab, complete thought, target not jammed against an edge."""
    words = _WORD_RE.findall(text)
    n = len(words)
    if n == 0:
        return -1e9
    s = -abs(n - 10) * 0.5
    if n < 5:
        s -= 7
    if n > 20:
        s -= (n - 20) * 1.6
    tlem = lemma_key(span)
    ranks = []
    for w in words:
        lk = lemma_key(w)
        if lk == tlem:
            continue
        ranks.append(rank_of.get(lk) or 55000)
    if ranks:
        s -= sum(1 for r in ranks if r > 25000) * 2.2       # too many other rare words
        s -= (sum(ranks) / len(ranks)) / 9000
    if text[-1:] in ".!?…":
        s += 3
    if text[:1].isupper():
        s += 1.5
    if _STARTS_MID.match(norm(text)):
        s -= 2.5
    low = [norm(w) for w in words]
    pos = next((k for k, w in enumerate(low)
                if norm(span) in w or any(w.startswith(st) for st in stems)), None)
    if pos is not None and 1 <= pos <= n - 2:
        s += 2
    return s


def word_occurrences(lemma, per_video=10):
    """Every place `lemma` (any inflection) is spoken, grouped by video. Reads
    the cached per-video inverted index — no transcript scan.
    -> [{video_id, title, youtube_id, thumbnail_url, count, hits:[{t,text}]}]."""
    lemma = lemma_key(lemma)
    c = connect()
    vids = c.execute(
        "SELECT id, title, url, thumbnail_url FROM videos ORDER BY id DESC").fetchall()
    c.close()
    out = []
    for v in vids:
        texts, times, idx = _lemma_index(v["id"])
        lines = sorted(set(idx.get(lemma, ())))
        if not lines:
            continue
        hits, last = [], -9
        for i in lines:
            if i - last >= 2:
                hits.append({"t": times[i][:8], "text": texts[i].strip()})
            last = i
        out.append({
            "video_id": v["id"], "title": v["title"],
            "youtube_id": youtube_id(v["url"]),
            "thumbnail_url": _thumb(v["url"], v["thumbnail_url"]),
            "count": len(hits), "hits": hits[:per_video]})
    return out


def word_status(lemma):
    lemma = norm(lemma)
    c = connect()
    cand = c.execute(
        """SELECT id, video_id, span_text, translation, status, timestamp_start
           FROM candidates WHERE normalized_text=? ORDER BY id DESC LIMIT 1""",
        (lemma,)).fetchone()
    fam = c.execute("SELECT root FROM word_family WHERE lemma=?", (lemma,)).fetchone()
    members = []
    if fam:
        members = [r["lemma"] for r in c.execute(
            "SELECT lemma FROM word_family WHERE root=? ORDER BY lemma", (fam["root"],))]
    c.close()
    return dict(cand) if cand else None, members


_TS_CYR = _re.compile(r"[А-Яа-яЁё][А-Яа-яЁё-]*")
_LEMMA_IDX = {}          # video_id -> (line_texts, line_times, {lemma: [line idx]})


def _lemma_index(video_id):
    """Cached inverted index for one video's transcript: lemma -> line indices,
    built with the (memoised) lemmatiser once and reused by every count / search
    / sentence-picker path. Invalidated whenever the lines are rewritten."""
    got = _LEMMA_IDX.get(video_id)
    if got is not None:
        return got
    c = connect()
    rows = c.execute(
        "SELECT start_time, text FROM subtitle_lines WHERE video_id=? "
        "AND text NOT LIKE '## %' ORDER BY id", (video_id,)).fetchall()
    c.close()
    texts = [r["text"] or "" for r in rows]
    times = [(r["start_time"] or "") for r in rows]
    idx = {}
    for i, t in enumerate(texts):
        for tok in _TS_CYR.findall(t):
            idx.setdefault(lemma_key(tok), []).append(i)
    got = _LEMMA_IDX[video_id] = (texts, times, idx)
    return got


def _drop_lemma_index(video_id):
    _LEMMA_IDX.pop(video_id, None)


def transcript_texts(video_id):
    return list(_lemma_index(video_id)[0])


def occurrence_count(lemma, video_id):
    """How many transcript lines contain `lemma` (any inflection)."""
    return len(set(_lemma_index(video_id)[2].get(lemma_key(lemma), ())))


_HMS_RE = _re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)")


def _to_secs(hms):
    m = _HMS_RE.match(str(hms or ""))
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def secs_to_hms(s):
    s = max(0.0, float(s))
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


def _locate_line(video_id, sentence, normalized_text, approx_sec):
    """Index of the transcript line where this card's moment actually is: the
    line that best matches the card's SENTENCE, falling back to the nearest
    occurrence of the lemma, then to the line nearest `approx_sec`.
    -> (line_index, secs_list) or (None, secs_list)."""
    texts, times, idx = _lemma_index(video_id)
    secs = [_to_secs(t) for t in times]
    valid = [i for i, s in enumerate(secs) if s is not None]
    if not valid:
        return None, secs
    lem_lines = set(idx.get(lemma_key(normalized_text or ""), []))
    sent_stems = [s for s in _stems(sentence or "") if len(s) >= 3]
    best, best_score = None, 1.5           # need a real match to override
    for i in valid:
        if not sent_stems:
            break
        toks = _re.split(r"[^а-яёa-z-]+", norm(texts[i]))
        overlap = sum(1 for st in sent_stems
                      if any(tok.startswith(st) for tok in toks if tok))
        if not overlap:
            continue
        score = overlap + (1.2 if i in lem_lines else 0) - abs(secs[i] - approx_sec) / 600.0
        if score > best_score:
            best, best_score = i, score
    if best is not None:
        return best, secs
    if lem_lines:
        cand = [i for i in lem_lines if secs[i] is not None]
        if cand:
            return min(cand, key=lambda i: abs(secs[i] - approx_sec)), secs
    return min(valid, key=lambda i: abs(secs[i] - approx_sec)), secs


def locate_seconds(video_id, sentence, normalized_text, approx_sec):
    """Corrected timestamp (seconds) for a card, or approx_sec if unlocatable."""
    try:
        i, secs = _locate_line(video_id, sentence, normalized_text, approx_sec)
    except Exception:  # noqa: BLE001
        return approx_sec
    return secs[i] if i is not None else approx_sec


def resnap_candidates(video_id):
    """Re-locate each pending candidate's timestamp against the current
    transcript (after the lyrics/subs were refreshed). Returns rows changed."""
    c = connect()
    rows = c.execute(
        "SELECT id, span_text, normalized_text, sentence, timestamp_start "
        "FROM candidates WHERE video_id=? AND status='pending'", (video_id,)).fetchall()
    changed = 0
    for r in rows:
        approx = _to_secs(r["timestamp_start"]) if r["timestamp_start"] else 0.0
        secs = locate_seconds(video_id, r["sentence"] or "",
                              r["normalized_text"] or r["span_text"], approx)
        if secs is not None and abs(secs - approx) > 1.5:
            c.execute("UPDATE candidates SET timestamp_start=? WHERE id=?",
                      (secs_to_hms(secs)[:8], r["id"]))
            changed += 1
    c.commit()
    c.close()
    return changed


def clip_window(video_id, normalized_text, approx_sec, sentence="", lead=1.2, trail=1.5):
    """Where the audio clip for a card should start/run — snapped to the line
    that best matches the card's sentence / really contains the lemma, spanning
    that line plus a little trailing context. -> (start_sec, duration_sec)."""
    try:
        i, secs = _locate_line(video_id, sentence, normalized_text, approx_sec)
    except Exception:  # noqa: BLE001
        return max(0.0, approx_sec - lead), lead + 5.0
    if i is None:
        return max(0.0, approx_sec - lead), lead + 5.0
    hit = i
    start = max(0.0, secs[hit] - lead)
    # end = start of the line two positions on, if that's a sane gap; else fixed
    nxt = next((secs[j] for j in range(hit + 1, min(hit + 3, len(secs)))
                if secs[j] is not None and secs[j] > secs[hit]), None)
    end = (nxt + trail) if (nxt and nxt - secs[hit] <= 10) else secs[hit] + 6.0
    dur = min(12.0, max(3.0, end - start))
    return start, dur


def rank_map(words):
    """{lemma: freq rank} for a set of lemmas, one query."""
    words = {w for w in words if w}
    out = {}
    if not words:
        return out
    c = connect()
    q = ",".join("?" * len(words))
    for r in c.execute(
        f"SELECT normalized_text, rank FROM freq WHERE normalized_text IN ({q})",
        tuple(words),
    ):
        out[r["normalized_text"]] = r["rank"]
    c.close()
    return out


def score_sentence(text, span):
    """Public: higher = better flashcard context."""
    stems = _stems(span)
    ranks = rank_map({lemma_key(w) for w in _WORD_RE.findall(text)})
    return round(_score_sentence(text, span, stems, ranks), 1)


def candidate_windows(cand_id, limit=6):
    """Raw context windows around each distinct occurrence of the candidate's
    word across the video — for the LLM to clean into flashcard sentences.
    -> [{"raw": str, "timestamp": "HH:MM:SS"}]."""
    cand = get_candidate(cand_id)
    if not cand:
        return []
    span = cand["span_text"]
    texts, times, idx = _lemma_index(cand["video_id"])
    lines = sorted(set(idx.get(lemma_key(span), ())))

    out, last = [], -99
    for i in lines:
        if i - last < 3:
            continue
        raw = _re.sub(r"\s+", " ",
                      " ".join(texts[max(0, i - 2):i + 3])).strip()
        out.append({"raw": raw, "timestamp": (times[i] or "")[:8]})
        last = i
        if len(out) >= limit:
            break
    return out


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

def in_stoplist(normalized):
    c = connect()
    hit = c.execute("SELECT 1 FROM stoplist WHERE normalized_text=?",
                    (norm(normalized),)).fetchone()
    c.close()
    return bool(hit)


def exclusion_reason(c, normalized):
    if c.execute("SELECT 1 FROM stoplist WHERE normalized_text=?", (normalized,)).fetchone():
        return "stoplist"
    r = c.execute(
        "SELECT reason FROM resolved_words WHERE normalized_text=?", (normalized,)
    ).fetchone()
    return r["reason"] if r else None


def freq_hint(normalized_text, is_phrase=False):
    """How rare is this word — a judgment aid during review. Review candidates
    are all past the 13k stoplist, so the bands start there."""
    if is_phrase or " " in (normalized_text or ""):
        return {"rank": None, "label": "phrase", "band": "rare"}
    c = connect()
    r = c.execute("SELECT rank FROM freq WHERE normalized_text=?", (normalized_text,)).fetchone()
    c.close()
    if not r:
        return {"rank": None, "label": "very rare", "band": "rare"}
    rank = r["rank"]
    if rank <= 16000:
        band, label = "mid", "borderline"      # near what you already know
    elif rank <= 32000:
        band, label = "uncommon", "uncommon"
    else:
        band, label = "rare", "rare"
    return {"rank": rank, "label": f"{label} · ~#{rank // 1000}k", "band": band}


def glosses_for(lemmas):
    """{lemma: gloss} for the ones the local dictionary knows — one query.
    Feeds the offline gloss pack shipped with /watch and /read."""
    lemmas = [l for l in {(x or "").strip().lower().replace("ё", "е") for x in lemmas} if l]
    if not lemmas:
        return {}
    c = connect()
    out = {}
    try:
        for chunk in (lemmas[i:i + 800] for i in range(0, len(lemmas), 800)):
            q = "SELECT headword, gloss FROM dict_ru WHERE headword IN (%s)" % \
                ",".join("?" * len(chunk))
            for r in c.execute(q, chunk):
                out[r["headword"]] = r["gloss"]
    except sqlite3.OperationalError:
        pass
    c.close()
    return out


def gloss_for(span):
    """Instant best-effort Russian->English gloss from the local dictionary, or
    None. Tries the surface form, then the pymorphy lemma. Placeholder only —
    the card is always LLM-translated."""
    s = (span or "").strip().lower().replace("ё", "е")
    if not s:
        return None
    c = connect()
    try:
        row = c.execute("SELECT gloss FROM dict_ru WHERE headword=?", (s,)).fetchone()
        if not row:
            lem = lemma_key(s)
            if lem != s:
                row = c.execute("SELECT gloss FROM dict_ru WHERE headword=?", (lem,)).fetchone()
        return row["gloss"] if row else None
    except sqlite3.OperationalError:      # table not built yet
        return None
    finally:
        c.close()


_CYR_TOKEN = _TS_CYR


def lemma_counts(video_id):
    """lemma -> how many transcript lines it appears in — recurrence signal for
    ranking review candidates. Reads the cached inverted index."""
    idx = _lemma_index(video_id)[2]
    return {lem: len(set(lines)) for lem, lines in idx.items()}


def notable_recurring(video_id, min_count=4, limit=20):
    """Lemmas spoken >= min_count times that AREN'T in the stoplist and haven't
    been decided — words this specific video leans on that the generic extractor
    might skip (character-name-adjacent nouns, borderline terms). -> [(lemma, n)]."""
    counts = lemma_counts(video_id)
    c = connect()
    out = []
    for lem, n in sorted(counts.items(), key=lambda x: -x[1]):
        if n < min_count:
            break
        if len(lem) < 4 or "-" in lem:
            continue
        if exclusion_reason(c, lem):        # in stoplist, or already known/carded
            continue
        out.append((lem, n))
        if len(out) >= limit:
            break
    c.close()
    return out


def card_lemmas():
    """normalized_text of every word/phrase you have an Anki card for."""
    c = connect()
    rows = c.execute(
        "SELECT normalized_text FROM resolved_words WHERE reason='has_card'"
    ).fetchall()
    c.close()
    return {r["normalized_text"] for r in rows}


def carded_glosses():
    """{normalized_text: translation} for words you have a card for — the SRS
    card's back wins, else the candidate's. For the instant popover in /watch."""
    c = connect()
    out = {}
    try:
        for r in c.execute("SELECT normalized_text, translation FROM candidates "
                           "WHERE status='card_created' AND translation IS NOT NULL "
                           "AND translation != ''"):
            out[r["normalized_text"]] = r["translation"]
    except sqlite3.OperationalError:
        pass
    try:
        for r in c.execute("SELECT normalized_text, translation FROM srs_cards "
                           "WHERE translation IS NOT NULL AND translation != ''"):
            out[r["normalized_text"]] = r["translation"]
    except sqlite3.OperationalError:
        pass
    try:
        for r in c.execute("SELECT lemma, gloss FROM word_gloss"):
            out.setdefault(r["lemma"], r["gloss"])
    except sqlite3.OperationalError:
        pass
    c.close()
    return out


def carded_accents():
    """{normalized_text: (surface_stressed, dict_stressed)} for words you have a
    card for — feeds the stress hint in the inline reader/watch popover."""
    c = connect()
    out = {}
    try:
        for r in c.execute(
            "SELECT normalized_text, accented, dict_accented FROM srs_cards "
            "WHERE (accented IS NOT NULL AND accented != '') "
            "   OR (dict_accented IS NOT NULL AND dict_accented != '')"):
            out.setdefault(r["normalized_text"],
                           (r["accented"] or "", r["dict_accented"] or ""))
    except sqlite3.OperationalError:
        pass
    c.close()
    return out


def known_family_lemmas():
    """Every lemma that shares a word-formation family with something you've
    carded — so работа / рабочий / работник all count as 'known' once you have
    работать. (card_lemmas() is a subset once families are learned.)"""
    c = connect()
    try:
        rows = c.execute(
            """SELECT wf.lemma FROM word_family wf WHERE wf.root IN (
                 SELECT w2.root FROM word_family w2
                 JOIN resolved_words r ON r.normalized_text = w2.lemma
                                      AND r.reason = 'has_card')
               AND wf.lemma NOT IN (SELECT normalized_text FROM stoplist)"""
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    c.close()
    return {r["lemma"] for r in rows}


def set_word_family(root, lemmas):
    lemmas = {lemmas} if isinstance(lemmas, str) else set(lemmas)
    lemmas = {norm(m) for m in lemmas if m}
    if not lemmas:
        return
    c = connect()
    # never pull a stoplist word (что, это, как, сказать, …) into a family — an
    # over-eager LLM grouping would otherwise light up a function word as "known"
    # everywhere.
    stop = {r["normalized_text"] for r in c.execute(
        "SELECT normalized_text FROM stoplist WHERE normalized_text IN (%s)"
        % ",".join("?" * len(lemmas)), tuple(lemmas))}
    lemmas -= stop
    if not lemmas:
        c.close()
        return
    c.executemany("INSERT OR REPLACE INTO word_family(lemma, root) VALUES(?,?)",
                  [(m, norm(root) or next(iter(lemmas))) for m in lemmas])
    c.commit()
    c.close()


def set_word_state(lemma, reason="known"):
    """Record the learner's verdict on a lemma (`wordstate.STATES` — 'learned',
    'known', …). Stops highlighting it everywhere, breaks any word-family link,
    keeps extraction from re-proposing it, and returns the Anki note ids of any
    pipeline-made cards so the caller can delete them.
    -> {lemma, reason, was_family, removed_notes}."""
    lemma = norm(lemma)
    c = connect()
    was_family = bool(c.execute(
        "SELECT 1 FROM word_family WHERE lemma=?", (lemma,)).fetchone())
    c.execute("DELETE FROM word_family WHERE lemma=?", (lemma,))
    notes = [r["anki_note_id"] for r in c.execute(
        "SELECT anki_note_id FROM candidates "
        "WHERE normalized_text=? AND status='card_created' AND anki_note_id IS NOT NULL",
        (lemma,))]
    c.execute("UPDATE candidates SET status='discarded', anki_note_id=NULL "
              "WHERE normalized_text=? AND status IN ('pending','card_created')",
              (lemma,))
    c.execute(
        """INSERT INTO resolved_words(normalized_text, reason, video_id)
           VALUES(?, ?, NULL)
           ON CONFLICT(normalized_text) DO UPDATE SET
             reason=excluded.reason, resolved_at=datetime('now')""",
        (lemma, reason))
    c.commit()
    c.close()
    return {"lemma": lemma, "reason": reason,
            "was_family": was_family, "removed_notes": notes}


def discard_word(lemma):
    """Back-compat alias: 'not a word I'm learning'."""
    return set_word_state(lemma, "known")


def clear_word_state(lemma):
    """Undo a verdict — the word goes back to undecided (can be suggested /
    highlighted again). Does NOT recreate any deleted card."""
    lemma = norm(lemma)
    c = connect()
    n = c.execute("DELETE FROM resolved_words WHERE normalized_text=? "
                  "AND reason != 'has_card'", (lemma,)).rowcount
    c.execute("UPDATE candidates SET status='pending' "
              "WHERE normalized_text=? AND status='discarded'", (lemma,))
    c.commit()
    c.close()
    return {"lemma": lemma, "cleared": bool(n)}


def word_state_counts():
    """{reason: n} across every decided lemma — for the progress screen."""
    c = connect()
    rows = c.execute(
        "SELECT reason, COUNT(*) n FROM resolved_words GROUP BY reason").fetchall()
    c.close()
    return {r["reason"]: r["n"] for r in rows}


def words_in_state(reason, limit=2000):
    """Lemmas the learner put in `reason`, newest verdict first, with a gloss
    when we have one cached."""
    c = connect()
    rows = c.execute(
        """SELECT r.normalized_text lemma, r.resolved_at,
                  COALESCE(g.gloss,
                    (SELECT translation FROM candidates
                     WHERE normalized_text=r.normalized_text AND translation<>''
                     ORDER BY id DESC LIMIT 1)) gloss
           FROM resolved_words r
           LEFT JOIN word_gloss g ON g.lemma = r.normalized_text
           WHERE r.reason = ?
           ORDER BY r.resolved_at DESC LIMIT ?""",
        (reason, limit)).fetchall()
    c.close()
    return [dict(x) for x in rows]


def yo_form(lemma):
    """Citation spelling with ё restored (полет -> полёт). Instant, no LLM."""
    lemma = norm(lemma)
    if " " in lemma or "-" in lemma:
        return lemma
    try:
        return _legacy.yo_lemma(lemma)
    except Exception:  # noqa: BLE001
        return lemma


def accent_for(lemma):
    """Cached stressed form, or None."""
    c = connect()
    try:
        r = c.execute("SELECT accented FROM word_accent WHERE lemma=?",
                      (norm(lemma),)).fetchone()
    except sqlite3.OperationalError:
        r = None
    c.close()
    return r["accented"] if r else None


def set_accent(lemma, accented):
    if not accented:
        return
    c = connect()
    c.execute("INSERT OR REPLACE INTO word_accent(lemma, accented) VALUES(?,?)",
              (norm(lemma), accented.strip()))
    c.commit()
    c.close()


def word_gloss_get(lemma):
    c = connect()
    try:
        r = c.execute("SELECT gloss FROM word_gloss WHERE lemma=?",
                      (norm(lemma),)).fetchone()
    except sqlite3.OperationalError:
        r = None
    c.close()
    return r["gloss"] if r else None


def word_gloss_set(lemma, gloss):
    if not gloss:
        return
    c = connect()
    c.execute("INSERT OR REPLACE INTO word_gloss(lemma, gloss) VALUES(?,?)",
              (norm(lemma), gloss.strip()))
    c.commit()
    c.close()


def lemmas_without_family():
    """Carded lemmas that don't yet have a word_family entry — for a one-time
    backfill."""
    c = connect()
    try:
        rows = c.execute(
            """SELECT normalized_text FROM resolved_words
               WHERE reason='has_card'
                 AND normalized_text NOT IN (SELECT lemma FROM word_family)
                 AND normalized_text NOT LIKE '% %'"""
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    c.close()
    return [r["normalized_text"] for r in rows]


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

def add_candidates(video_id, items, source="batch", family=None):
    """items: [{span_text, is_phrase, sentence, timestamp_start, translation}].
    Applies stoplist + resolved_words + word-family filter and in-video dedup.
    Returns (added, skipped[(span, reason)])."""
    family = family or set()
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
        if n in family:                       # same word-family as something carded
            skipped.append((span, "family"))
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


def mark_carded(span_text):
    """Record that a card now exists for this word/phrase (no candidate row).
    Used by the reading feature and any other direct card creation."""
    c = connect()
    c.execute(
        """INSERT INTO resolved_words(normalized_text, reason, video_id)
           VALUES(?, 'has_card', NULL)
           ON CONFLICT(normalized_text) DO UPDATE SET
             reason='has_card', resolved_at=datetime('now')""",
        (lemma_key((span_text or "").strip()),),
    )
    c.commit()
    c.close()


# ---------------------------------------------------------------- reading

def add_text(title, author, kind, chapters):
    """chapters: [{title, body}]. Returns text id."""
    c = connect()
    total = sum(len(ch["body"]) for ch in chapters)
    cur = c.execute(
        "INSERT INTO texts(title, author, kind, char_count) VALUES(?,?,?,?)",
        (title, author, kind, total))
    tid = cur.lastrowid
    c.executemany(
        "INSERT INTO text_chapters(text_id, idx, title, body) VALUES(?,?,?,?)",
        [(tid, i, ch.get("title"), ch["body"]) for i, ch in enumerate(chapters)])
    c.commit()
    c.close()
    return tid


def add_reading_text(url, title, author, chapters):
    """Store an imported reading text as a kind='text' video so the whole
    pipeline (extraction / cards / word pages / highlighting) applies. Each
    paragraph is one subtitle_line; chapter headings are '## …' lines.
    `chapters`: [{title, paragraphs: [str]}]. -> video id."""
    lines, vtt, t = [], ["WEBVTT", ""], 0.0
    for ci, ch in enumerate(chapters, 1):
        if ch.get("title"):
            lines.append((f"{ci}:00:00", "## " + ch["title"]))
        for pi, para in enumerate(ch.get("paragraphs") or [], 1):
            lines.append((f"{ci}:{pi // 60:02d}:{pi % 60:02d}", para))
            vtt += [f"{secs_to_hms(t)} --> {secs_to_hms(t + 4)}", para, ""]
            t += 5
    vid = upsert_video(url, title, "text", "ru", "\n".join(vtt), channel=author)
    c = connect()
    c.execute("UPDATE videos SET kind='text' WHERE id=?", (vid,))
    c.commit()
    c.close()
    replace_subtitle_lines(vid, lines)
    return vid


def append_reading_chapters(video_id, chapters, from_num):
    """Add more chapters to an existing kind='text' item without disturbing what's
    already there (reading position, cards, highlights all stay). `from_num` is
    the 1-based number of the first appended chapter. -> lines added."""
    new_lines, vtt_add = [], []
    t0 = 0.0
    for off, ch in enumerate(chapters):
        ci = from_num + off
        if ch.get("title"):
            new_lines.append((f"{ci}:00:00", "## " + ch["title"]))
        for pi, para in enumerate(ch.get("paragraphs") or [], 1):
            new_lines.append((f"{ci}:{pi // 60:02d}:{pi % 60:02d}", para))
            vtt_add += [f"{secs_to_hms(t0)} --> {secs_to_hms(t0 + 4)}", para, ""]
            t0 += 5
    c = connect()
    c.executemany(
        "INSERT INTO subtitle_lines(video_id, start_time, text) VALUES(?,?,?)",
        [(video_id, ts, tx) for ts, tx in new_lines])
    cur = c.execute("SELECT raw_subs FROM videos WHERE id=?", (video_id,)).fetchone()
    raw = (cur["raw_subs"] if cur else "") or "WEBVTT\n"
    c.execute("UPDATE videos SET raw_subs=? WHERE id=?",
              (raw.rstrip() + "\n\n" + "\n".join(vtt_add), video_id))
    c.commit()
    c.close()
    _LEMMA_IDX.pop(video_id, None)
    drop_sentence_cache(video_id=video_id)
    return len(new_lines)


def add_song(url, title, artist, cues, subs_kind="lrclib", duration=None,
             thumbnail_url=None):
    """Store a song as a kind='song' video. `cues`: [(start, end, text)] — the
    timed lyrics. VTT feeds the player; each lyric line is one subtitle_line so
    extraction / search / word pages work exactly as for a video."""
    vtt = ["WEBVTT", ""]
    for s, e, txt in cues:
        vtt += [f"{secs_to_hms(s)} --> {secs_to_hms(max(e, s + 0.8))}", txt, ""]
    raw = "\n".join(vtt)
    vid = upsert_video(url, title, subs_kind, "ru", raw,
                       channel=artist, thumbnail_url=thumbnail_url,
                       duration=duration)
    c = connect()
    c.execute("UPDATE videos SET kind='song' WHERE id=?", (vid,))
    c.commit()
    c.close()
    replace_subtitle_lines(vid, [(secs_to_hms(s), txt) for s, _, txt in cues])
    return vid


def reading_chapters(video_id):
    """[{n, title, first_line_id}] from the '## …' lines — the reader's TOC."""
    c = connect()
    rows = c.execute(
        "SELECT id, start_time, text FROM subtitle_lines WHERE video_id=? ORDER BY id",
        (video_id,)).fetchall()
    c.close()
    out, n = [], 0
    for r in rows:
        if (r["text"] or "").startswith("## "):
            n += 1
            out.append({"n": n, "title": r["text"][3:].strip(), "line_id": r["id"]})
    return out


def list_texts():
    c = connect()
    rows = c.execute(
        """SELECT t.*, (SELECT count(*) FROM text_chapters ch WHERE ch.text_id=t.id)
                       AS chapters
           FROM texts t ORDER BY t.added_at DESC""").fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_text(text_id):
    c = connect()
    t = c.execute("SELECT * FROM texts WHERE id=?", (text_id,)).fetchone()
    if not t:
        c.close()
        return None
    chs = c.execute(
        "SELECT idx, title, length(body) AS len FROM text_chapters "
        "WHERE text_id=? ORDER BY idx", (text_id,)).fetchall()
    c.close()
    return {**dict(t), "chapters": [dict(r) for r in chs]}


def get_chapter(text_id, idx):
    c = connect()
    r = c.execute(
        "SELECT idx, title, body FROM text_chapters WHERE text_id=? AND idx=?",
        (text_id, idx)).fetchone()
    c.close()
    return dict(r) if r else None


def delete_text(text_id):
    c = connect()
    c.execute("DELETE FROM text_chapters WHERE text_id=?", (text_id,))
    n = c.execute("DELETE FROM texts WHERE id=?", (text_id,)).rowcount
    c.commit()
    c.close()
    return n


def update_candidate_sentence(cand_id, sentence):
    c = connect()
    c.execute("UPDATE candidates SET sentence=? WHERE id=?", (sentence, cand_id))
    c.commit()
    c.close()
    drop_sentence_cache(cand_id=cand_id)


def get_sentence_cache(cand_id):
    c = connect()
    try:
        r = c.execute(
            "SELECT payload FROM candidate_sentences_cache WHERE candidate_id=?",
            (cand_id,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    c.close()
    if not r:
        return None
    try:
        return _json.loads(r["payload"])
    except ValueError:
        return None


def set_sentence_cache(cand_id, video_id, payload):
    c = connect()
    c.execute(
        "INSERT OR REPLACE INTO candidate_sentences_cache"
        "(candidate_id, video_id, payload) VALUES(?,?,?)",
        (cand_id, video_id, _json.dumps(payload)))
    c.commit()
    c.close()


def drop_sentence_cache(video_id=None, cand_id=None):
    c = connect()
    try:
        if cand_id is not None:
            c.execute("DELETE FROM candidate_sentences_cache WHERE candidate_id=?",
                      (cand_id,))
        if video_id is not None:
            c.execute("DELETE FROM candidate_sentences_cache WHERE video_id=?",
                      (video_id,))
        c.commit()
    except sqlite3.OperationalError:
        pass
    c.close()


def lyric_note_get(video_id, line_index):
    c = connect()
    try:
        r = c.execute("SELECT payload FROM lyric_notes WHERE video_id=? AND line_index=?",
                      (video_id, line_index)).fetchone()
    except sqlite3.OperationalError:
        r = None
    c.close()
    if not r:
        return None
    try:
        return _json.loads(r["payload"])
    except ValueError:
        return None


def lyric_note_set(video_id, line_index, payload):
    c = connect()
    c.execute("INSERT OR REPLACE INTO lyric_notes(video_id, line_index, payload) "
              "VALUES(?,?,?)", (video_id, line_index, _json.dumps(payload)))
    c.commit()
    c.close()


def drop_lyric_notes(video_id):
    c = connect()
    try:
        c.execute("DELETE FROM lyric_notes WHERE video_id=?", (video_id,))
        c.commit()
    except sqlite3.OperationalError:
        pass
    c.close()


def resolve_candidate(cand_id, decision, note_id=None, reason=None):
    """decision: 'yes' -> status card_created + resolved_words(has_card);
                 'no'  -> status discarded  + resolved_words(`reason` or 'known').
    `reason` lets a 'no' carry the learner's verdict ('learned' vs 'known').
    Returns the updated candidate row (dict)."""
    c = connect()
    row = c.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone()
    if not row:
        c.close()
        raise KeyError(cand_id)
    if decision == "yes":
        status, reason = "card_created", "has_card"
    else:
        status, reason = "discarded", (reason or "known")
    c.execute("UPDATE candidates SET status=?, anki_note_id=COALESCE(?, anki_note_id) WHERE id=?",
              (status, note_id, cand_id))
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


def unresolve_candidate(cand_id):
    """Put a decided candidate back to 'pending' and forget the decision — the
    undo for an inline card/skip made while watching. -> (row, freed_note_id)."""
    c = connect()
    row = c.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone()
    if not row:
        c.close()
        raise KeyError(cand_id)
    note_id = row["anki_note_id"]
    c.execute("UPDATE candidates SET status='pending', anki_note_id=NULL WHERE id=?", (cand_id,))
    c.execute("DELETE FROM resolved_words WHERE normalized_text=?", (row["normalized_text"],))
    c.commit()
    out = dict(c.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone())
    c.close()
    return out, note_id


def video_decided_lemmas(video_id):
    """{lemma: {'id', 'status', 'note_id'}} for this video's card_created /
    discarded candidates — lets the watch view offer an undo on a green word or
    a word you skipped."""
    c = connect()
    rows = c.execute(
        """SELECT id, normalized_text, status, anki_note_id FROM candidates
           WHERE video_id=? AND status IN ('card_created','discarded')""",
        (video_id,)).fetchall()
    c.close()
    return {r["normalized_text"]: {"id": r["id"], "status": r["status"],
                                   "note_id": r["anki_note_id"]} for r in rows}
