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

-- Every word/phrase that has been shown to the user and decided, either way.
-- Checked (with the static stoplist) before anything is flagged as a candidate
-- again. reason: 'known' | 'garbage' | 'has_card'.
CREATE TABLE IF NOT EXISTS resolved_words (
    normalized_text TEXT PRIMARY KEY,
    reason          TEXT NOT NULL,
    video_id        INTEGER REFERENCES videos(id),
    resolved_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
