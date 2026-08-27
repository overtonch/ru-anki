-- Russian vocab -> Anki pipeline. SQLite is the single source of truth.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    title       TEXT,
    subs_kind   TEXT,               -- 'manual' | 'auto'
    subs_lang   TEXT,
    raw_subs    TEXT,               -- raw subtitle file contents
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        INTEGER NOT NULL REFERENCES videos(id),
    span_text       TEXT NOT NULL,      -- the word or phrase as shown to the learner
    normalized_text TEXT NOT NULL,      -- lowercased, e -> e (yo folded), for matching
    is_phrase       INTEGER NOT NULL DEFAULT 0,
    sentence        TEXT NOT NULL,      -- source sentence from the video
    timestamp_start TEXT,               -- HH:MM:SS.mmm of the cue the span appears in
    translation     TEXT,              -- best contextual English translation
    status          TEXT NOT NULL DEFAULT 'pending',
                    -- pending | confirmed_unknown | confirmed_known | garbage
    stress          TEXT,              -- nice-to-have, left blank for v0
    exported_at     TEXT,              -- set when written into an .apkg
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_candidates_video  ON candidates(video_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_norm   ON candidates(normalized_text);

CREATE TABLE IF NOT EXISTS known_lexicon (
    normalized_text         TEXT PRIMARY KEY,
    span_text               TEXT,      -- a representative surface form, for display
    first_confirmed_video_id INTEGER REFERENCES videos(id),
    confirmed_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Static high-frequency backstop: never flag anything whose normalized form is here.
CREATE TABLE IF NOT EXISTS stoplist (
    normalized_text TEXT PRIMARY KEY,
    rank            INTEGER
);
