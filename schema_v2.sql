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

-- Every word/phrase that has been shown to the user and decided, either way.
-- Checked (with the static stoplist) before anything is flagged as a candidate
-- again. reason: 'known' | 'garbage' | 'has_card'.
CREATE TABLE IF NOT EXISTS resolved_words (
    normalized_text TEXT PRIMARY KEY,
    reason          TEXT NOT NULL,
    video_id        INTEGER REFERENCES videos(id),
    resolved_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
