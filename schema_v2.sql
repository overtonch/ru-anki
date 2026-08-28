-- Phase 0+ additive schema for the FastAPI server. Idempotent; runs on startup
-- alongside the original schema.sql. The original tables (videos, candidates,
-- stoplist, known_lexicon) are kept; this adds the plan's data model on top.

PRAGMA journal_mode = WAL;

-- Indexed transcript, populated as soon as a video is submitted (before any LLM
-- call). Backs the live word-search mode.
CREATE TABLE IF NOT EXISTS subtitle_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   INTEGER NOT NULL REFERENCES videos(id),
    text       TEXT NOT NULL,
    start_time TEXT                 -- 'HH:MM:SS'
);
CREATE INDEX IF NOT EXISTS idx_sublines_video ON subtitle_lines(video_id);

-- Full ~50k lemma frequency ranks (populated by build_stoplist.py), for the
-- review-time "how rare is this word" hint.
CREATE TABLE IF NOT EXISTS freq (
    normalized_text TEXT PRIMARY KEY,
    rank            INTEGER
);

-- Local Russian->English glosses (populated by build_dict.py from WikDict), for
-- the instant best-effort translation shown while the real LLM call runs. The
-- card is always LLM-translated; this is only a placeholder.
CREATE TABLE IF NOT EXISTS dict_ru (
    headword TEXT PRIMARY KEY,
    gloss    TEXT
);

-- Word-formation families: every lemma that shares a root + core meaning with a
-- word you've carded counts as "known" for highlighting + extraction, so you
-- don't get separate cards for работа / работать / рабочий. Populated by an LLM
-- call the first time a word in the family is carded.
CREATE TABLE IF NOT EXISTS word_family (
    lemma TEXT PRIMARY KEY,
    root  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_word_family_root ON word_family(root);

-- Stress ("ударение") + ё spelling for a lemma, shown on the card back and the
-- word page as a reference hint — never in the transcript (reading practice is
-- meant to happen without accent marks). Filled lazily by an LLM call the first
-- time a word is carded or its word page is opened.
CREATE TABLE IF NOT EXISTS word_accent (
    lemma    TEXT PRIMARY KEY,
    accented TEXT NOT NULL,
    made_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Lazily-filled English gloss for a carded word that has no candidate / srs_card
-- to read a translation from (orphans from the pre-SRS Anki-only era). Shown in
-- the in-watch popover and the word page.
CREATE TABLE IF NOT EXISTS word_gloss (
    lemma   TEXT PRIMARY KEY,
    gloss   TEXT NOT NULL,
    made_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Memoised output of the sentence-picker (GET /candidates/{id}/sentences): the
-- LLM-cleaned + ranked flashcard-sentence options for one candidate. Dropped
-- when the video's transcript changes or the candidate's sentence is edited.
CREATE TABLE IF NOT EXISTS candidate_sentences_cache (
    candidate_id INTEGER PRIMARY KEY,
    video_id     INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    made_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cand_sent_cache_video ON candidate_sentences_cache(video_id);

-- Reading feature: imported long-form text (EPUB / .txt / pasted). Tap-to-card
-- while reading reuses the same translate + Anki pipeline as the video watcher.
CREATE TABLE IF NOT EXISTS texts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    author     TEXT,
    kind       TEXT,                     -- 'epub' | 'txt' | 'paste'
    char_count INTEGER NOT NULL DEFAULT 0,
    added_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS text_chapters (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    text_id INTEGER NOT NULL REFERENCES texts(id),
    idx     INTEGER NOT NULL,
    title   TEXT,
    body    TEXT NOT NULL                -- plain text, paragraphs split by blank line
);
CREATE INDEX IF NOT EXISTS idx_text_chapters ON text_chapters(text_id, idx);

-- In-app spaced repetition (FSRS). SQLite is the source of truth; Anki becomes
-- an optional dual-write target + an .apkg escape hatch. One row per study card.
CREATE TABLE IF NOT EXISTS srs_cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER UNIQUE REFERENCES candidates(id),  -- provenance / dedup
    sentence        TEXT NOT NULL,       -- raw source sentence (front rendered on read)
    translation     TEXT,                -- back
    span_text       TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    is_phrase       INTEGER NOT NULL DEFAULT 0,
    accented        TEXT,
    video_id        INTEGER REFERENCES videos(id),
    timestamp       TEXT,                -- HH:MM:SS.mmm — frame thumbnail + jump-to-moment
    -- FSRS state (see fsrs.Card.to_dict)
    fsrs_state      INTEGER NOT NULL DEFAULT 1,   -- 1 learning, 2 review, 3 relearning
    fsrs_step       INTEGER,
    stability       REAL,
    difficulty      REAL,
    due             TEXT NOT NULL,       -- ISO8601 UTC
    last_review     TEXT,                -- ISO8601 UTC; NULL => never studied (a "new" card)
    reps            INTEGER NOT NULL DEFAULT 0,
    lapses          INTEGER NOT NULL DEFAULT 0,
    suspended       INTEGER NOT NULL DEFAULT 0,
    anki_note_id    INTEGER,             -- set if also dual-written to Anki
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_srs_due  ON srs_cards(due);
CREATE INDEX IF NOT EXISTS idx_srs_norm ON srs_cards(normalized_text);

CREATE TABLE IF NOT EXISTS srs_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id      INTEGER NOT NULL REFERENCES srs_cards(id),
    rating       INTEGER NOT NULL,       -- 1 Again, 2 Hard, 3 Good, 4 Easy
    -- card state BEFORE this review, for undo
    prev_state   INTEGER,
    prev_step    INTEGER,
    prev_stability REAL,
    prev_difficulty REAL,
    prev_due     TEXT,
    prev_last_review TEXT,
    reviewed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    elapsed_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_srs_reviews_card ON srs_reviews(card_id);
CREATE INDEX IF NOT EXISTS idx_srs_reviews_when ON srs_reviews(reviewed_at);

-- tiny key/value bag for app-level settings (e.g. anki_dual_write)
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Every word/phrase that has been shown to the user and decided, either way.
-- Checked (with the static stoplist) before anything is flagged as a candidate
-- again. reason: 'known' | 'garbage' | 'has_card'.
CREATE TABLE IF NOT EXISTS resolved_words (
    normalized_text TEXT PRIMARY KEY,
    reason          TEXT NOT NULL,
    video_id        INTEGER REFERENCES videos(id),
    resolved_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
