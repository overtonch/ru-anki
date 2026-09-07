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

-- Deep line-by-line lyric explanations (POST /songs/{id}/explain): translation +
-- what's being expressed + wordplay / entendres / references, for one lyric line
-- in the context of the whole song. Memoised so re-taps are instant and free.
CREATE TABLE IF NOT EXISTS lyric_notes (
    video_id   INTEGER NOT NULL,
    line_index INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    made_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (video_id, line_index)
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
    accented        TEXT,                -- target word stressed AS IT APPEARS on the card
    dict_accented   TEXT,                -- stressed dictionary / citation form
    front_word      TEXT,                -- dict form / common phrase form; front when card_front='word'
    learn_score     INTEGER,             -- 0-100, legacy single-factor introduce score
    priority        REAL,                -- 0-100 multi-factor introduce-next score (see srs.score_new_cards)
    priority_meta   TEXT,                -- JSON: {speak, daily, fiction, freq, recency}
    source          TEXT,                -- NULL/'video'/'text' = pipeline; 'manual' = hand-added
    alt_meanings    TEXT,                -- other senses / idioms (small text on the back)
    sentence_full   TEXT,                -- original long context, before it was trimmed to the clause
    format_ver      INTEGER NOT NULL DEFAULT 1,   -- 2 once refined to the clean-primary + alts format
    meaning_contextual INTEGER NOT NULL DEFAULT 0, -- 1 = `translation` is the narrow in-context sense
    card_type       TEXT NOT NULL DEFAULT 'recognition', -- 'recognition' | 'production' (say-it-in-Russian)
    speak_ref       TEXT,                -- 'prompt:<id>' / 'correction:<id>' / 'drill:<id>' — provenance of a production card
    card_meta       TEXT,                -- JSON extras for production cards (given chips, target span, contrast, skill)
    -- (videos.lrc_offset lives on the videos table, added via ALTER in init_db)
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

-- ===================================================================
-- Reformulation-based speaking practice (see app/speak.py)
-- Active-production drill: the LLM hands you a concrete thought to say in
-- Russian; you attempt it (typed or spoken); the LLM gives 2-4 native
-- reformulations of the ORIGINAL thought + a tiered diff of your attempt; you
-- turn the gaps into production cards (srs_cards.card_type='production').
-- ===================================================================

CREATE TABLE IF NOT EXISTS speak_prompts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,                 -- the thought to express (English)
    hint       TEXT,                          -- what makes it resist word-for-word (shown after)
    level      TEXT NOT NULL DEFAULT 'a2',    -- b1 | b2 | c1 — target speaking difficulty
    source     TEXT NOT NULL DEFAULT 'llm',   -- 'llm' | 'user'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS speak_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id    INTEGER NOT NULL REFERENCES speak_prompts(id),
    user_text    TEXT NOT NULL,
    input_method TEXT NOT NULL DEFAULT 'typed',   -- 'typed' | 'stt'
    status       TEXT NOT NULL DEFAULT 'grading', -- 'grading' | 'done' | 'error'
    general_note TEXT,                            -- freeform LLM notes on this attempt's patterns
    meaning      TEXT,                            -- 'ok' | 'drifted'
    native       TEXT,                            -- the one native version of the thought
    native_gloss TEXT,                            -- literal-ish English of `native`
    error        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_speak_attempts_prompt ON speak_attempts(prompt_id);

CREATE TABLE IF NOT EXISTS speak_reformulations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  INTEGER NOT NULL REFERENCES speak_attempts(id),
    idx         INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,                    -- a native way to express the ORIGINAL thought
    register    TEXT,                             -- 'neutral' | 'colloquial' | 'formal'
    gloss       TEXT,                             -- literal-ish English, to help the user pick
    is_selected INTEGER NOT NULL DEFAULT 0        -- the user's "this feels like me" pick
);
CREATE INDEX IF NOT EXISTS idx_speak_reforms_attempt ON speak_reformulations(attempt_id);

CREATE TABLE IF NOT EXISTS speak_corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  INTEGER NOT NULL REFERENCES speak_attempts(id),
    idx         INTEGER NOT NULL DEFAULT 0,
    tier        TEXT NOT NULL,                    -- 'lexical' | 'grammar' | 'clarity'
    category    TEXT NOT NULL,                    -- aspect|case|word-order|agreement|conjugation|lexical|spelling|clarity|preposition|other
    original    TEXT,                             -- the learner's span ('' = omission)
    corrected   TEXT,                             -- the native span ('' = deletion)
    explanation TEXT,                             -- the rule / why it was unclear
    severity    TEXT NOT NULL DEFAULT 'hard',     -- 'hard' (wrong in every reformulation) | 'style' (fine but not native)
    card_id     INTEGER REFERENCES srs_cards(id), -- set once the user makes a card from it
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_speak_corr_attempt  ON speak_corrections(attempt_id);
CREATE INDEX IF NOT EXISTS idx_speak_corr_category ON speak_corrections(category);

-- how the attempt reads chunk-by-chunk, for the inline strikethrough→suggestion
-- rendering. Each row is either a verbatim run of the attempt (correction_idx
-- NULL) or a span that a correction replaces (correction_idx → speak_corrections.idx).
CREATE TABLE IF NOT EXISTS speak_diff (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id     INTEGER NOT NULL REFERENCES speak_attempts(id),
    seq            INTEGER NOT NULL,
    text           TEXT NOT NULL DEFAULT '',      -- the learner's words for this chunk
    correction_idx INTEGER                        -- NULL = unchanged run
);
CREATE INDEX IF NOT EXISTS idx_speak_diff_attempt ON speak_diff(attempt_id);

-- ===================================================================
-- Grammar drill (app/drill.py) — an endless self-graded flip-through for
-- conjugation / declension / aspect, walking a frequency band. Cards are
-- LLM-generated in rolling background batches; not FSRS-scheduled. The learner
-- can promote one to a real production card (srs_cards.card_type='production').
-- Band position + streaks live in app_settings (drill_band / drill_streak_*).
-- ===================================================================
-- One row per (lemma, skill) the learner has missed — a running remediation list.
-- Re-tested a few cards later; escalates to targeted cards on repeat misses;
-- retired on a clean hit.
CREATE TABLE IF NOT EXISTS drill_lapse (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lemma      TEXT,
    skill      TEXT,
    misses     INTEGER NOT NULL DEFAULT 0,
    clears     INTEGER NOT NULL DEFAULT 0,
    resolved   INTEGER NOT NULL DEFAULT 0,
    due_pos    INTEGER NOT NULL DEFAULT 0,      -- re-test once drill_pos reaches this
    src_item_id INTEGER,                        -- the drill_items row first missed
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(lemma, skill)
);
CREATE INDEX IF NOT EXISTS idx_drill_lapse_due ON drill_lapse(resolved, due_pos);

CREATE TABLE IF NOT EXISTS drill_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    band       INTEGER NOT NULL,               -- index into drill.BANDS at generation
    lemma      TEXT,                            -- the frequency-list word it drills
    kind       TEXT,                            -- 'government' | 'conjugation' | 'aspect' | 'declension'
    skill      TEXT,                            -- fine slug for progress tracking (aspect:pf, case:dative, …)
    prompt     TEXT NOT NULL,                   -- the English cue (front)
    given      TEXT,                            -- JSON list: dictionary-form words to build from
    answer     TEXT NOT NULL,                   -- the Russian to produce (back)
    target     TEXT,                            -- JSON list: exact answer substrings that are the graded criterion
    note       TEXT,                            -- why these endings / this aspect
    contrast   TEXT,                            -- aspect cards: the other aspect in this situation
    retest_for INTEGER REFERENCES drill_lapse(id),-- set if this card is a spaced re-test of a miss
    focus      INTEGER NOT NULL DEFAULT 0,      -- 1 = a 'learn this rule' burst; jumps the queue
    verdict    TEXT,                            -- NULL = not yet seen, 'right' | 'wrong'
    card_id    INTEGER REFERENCES srs_cards(id),-- set if promoted to a production card
    served_at  TEXT,
    graded_at  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_drill_unseen ON drill_items(verdict, id);

-- ===================================================================
-- Verbs-of-motion drill (app/motion.py) — same shape as the grammar drill but a
-- fixed taxonomy (verb pair × aspect/direction × prefix × preposition × tense).
-- The card front is an English scene with ONE clause highlighted; the learner
-- translates just that clause. Stats slice by any dimension.
-- ===================================================================
CREATE TABLE IF NOT EXISTS motion_lapse (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    combo       TEXT,                          -- the missed combination signature
    misses      INTEGER NOT NULL DEFAULT 0,
    resolved    INTEGER NOT NULL DEFAULT 0,
    due_pos     INTEGER NOT NULL DEFAULT 0,
    src_item_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(combo)
);
CREATE INDEX IF NOT EXISTS idx_motion_lapse_due ON motion_lapse(resolved, due_pos);

CREATE TABLE IF NOT EXISTS motion_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       INTEGER NOT NULL DEFAULT 1,
    situation   TEXT NOT NULL,                 -- the full English scene
    highlight   TEXT NOT NULL,                 -- the clause to translate (substring of situation)
    given       TEXT,                          -- JSON: subject / object nouns, nominative singular
    answer      TEXT NOT NULL,                 -- the Russian clause to produce
    target      TEXT,                          -- JSON: graded substring(s) of answer
    note        TEXT,
    contrast    TEXT,
    alts        TEXT,                          -- JSON [{form, why}] — plausible wrong choices
    dims        TEXT,                          -- JSON {verb, aspect, prefix, prep, tense}
    combo       TEXT,                          -- signature verb|aspect|prefix|prep|tense
    retest_for  INTEGER REFERENCES motion_lapse(id),
    focus       INTEGER NOT NULL DEFAULT 0,
    verdict     TEXT,
    card_id     INTEGER REFERENCES srs_cards(id),
    served_at   TEXT,
    graded_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_motion_unseen ON motion_items(verdict, id);

-- Reference pages linked from the back of a motion card, LLM-generated on first
-- request and cached. `motion_verb_ref`: one row per verb pair — both aspects,
-- full conjugation tables, notes, mistakes, examples, how prefixes combine.
-- `motion_prefix_ref`: one row per prefix — what it does to a motion, how to use
-- it, per-verb exceptions/idioms, common mistakes.
CREATE TABLE IF NOT EXISTS motion_verb_ref (
    verb_id     TEXT PRIMARY KEY,                -- app/motion.py VERBS id (idti, ehat, …)
    data        TEXT NOT NULL,                   -- JSON {summary, conj, mistakes, examples, prefixes, …}
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS motion_prefix_ref (
    prefix_id   TEXT PRIMARY KEY,                -- app/motion.py PREFIXES id (pri, u, za, …)
    data        TEXT NOT NULL,                   -- JSON {what_it_does, usage, exceptions, mistakes, examples}
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===================================================================
-- Chunk deck (app/chunks.py) — production drill for formulaic sequences.
-- A card is a casual English utterance with one conversational chunk highlighted;
-- you say the whole thing in Russian. Cards in `chunk_items`, misses in
-- `chunk_lapse`, the per-chunk fading-scaffold state in `chunk_stage`.
-- ===================================================================
CREATE TABLE IF NOT EXISTS chunk_stage (
    chunk_id    TEXT PRIMARY KEY,                 -- catalogue id (app/chunks.py CHUNKS)
    stage       INTEGER NOT NULL DEFAULT 0,       -- 0 literal … 3 cloze — how much English help
    streak      INTEGER NOT NULL DEFAULT 0,       -- consecutive rights at this stage
    seen        INTEGER NOT NULL DEFAULT 0,
    hits        INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunk_lapse (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id    TEXT,
    misses      INTEGER NOT NULL DEFAULT 0,
    resolved    INTEGER NOT NULL DEFAULT 0,
    due_pos     INTEGER NOT NULL DEFAULT 0,
    src_item_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_chunk_lapse_due ON chunk_lapse(resolved, due_pos);

-- ===================================================================
-- Speech Lab (app/speech.py) — a collection of longer pieces (a paragraph or
-- two) to memorise, shadow, and loop in the background. Text is either generated
-- from a brief or pasted in; natural-sounding Russian audio (Silero, app/tts_hq)
-- is built asynchronously. One row per speech.
-- ===================================================================
CREATE TABLE IF NOT EXISTS speeches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    topic          TEXT,                          -- the brief, if generated
    topic_id       TEXT,                          -- speech_topics id, if from "suggest one"
    source         TEXT NOT NULL DEFAULT 'generated',  -- 'generated' | 'pasted'
    ru             TEXT NOT NULL,                  -- the Russian text (paragraphs kept)
    en             TEXT,                           -- English translation / gloss
    notes          TEXT,                           -- JSON [{ru, why}] — key chunks / constructions
    status         TEXT NOT NULL DEFAULT 'audio',  -- audio | ready | error
    error          TEXT,
    audio_path     TEXT,
    voice          TEXT,
    practice_count INTEGER NOT NULL DEFAULT 0,
    last_practiced TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunk_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id    TEXT NOT NULL,
    fn          TEXT,                             -- conversational function id
    stage       INTEGER,                          -- scaffold stage this card was GRADED at
    en          TEXT NOT NULL,                    -- the full casual English utterance
    ru          TEXT NOT NULL,                    -- the full natural Russian
    chunk_en    TEXT,                             -- the English chunk portion
    chunk_ru    TEXT NOT NULL,                    -- the Russian chunk (verbatim substring of ru)
    gist        TEXT,                             -- a few bare words of the situation (recall stage)
    gloss       TEXT,                             -- word-for-word gloss of the chunk
    note        TEXT,
    target      TEXT,                             -- JSON: graded substring(s) of ru
    retest_for  INTEGER REFERENCES chunk_lapse(id),
    focus       INTEGER NOT NULL DEFAULT 0,
    verdict     TEXT,
    card_id     INTEGER REFERENCES srs_cards(id),
    served_at   TEXT,
    graded_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chunk_unseen ON chunk_items(verdict, id);

-- ===================================================================
-- Speaking journal (app/journal.py) — record a monologue "about your day",
-- code-switching to English where you don't know the Russian; local Whisper
-- transcribes, one LLM pass finds mistakes + translates the English gaps + drafts
-- cards. One row per recording; the analysis is a JSON blob.
-- ===================================================================
CREATE TABLE IF NOT EXISTS journal_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    audio_path  TEXT,
    duration    REAL,
    level       TEXT NOT NULL DEFAULT 'b1',
    transcript  TEXT,                            -- raw Whisper text, joined
    status      TEXT NOT NULL DEFAULT 'new',     -- new|transcribing|analyzing|done|error
    error       TEXT,
    analysis    TEXT,                            -- JSON {segments:[…], general, cards:[…]}
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===================================================================
-- Verb aspect (app/aspect.py) — lemma-keyed cache so a vocab card whose target
-- word is a verb can show its aspect + aspect partner on the back, linking to
-- the aspect explainer. Negatives are cached too (is_verb=0) so a noun is only
-- ever checked once. Regenerable — not in the git backup.
-- ===================================================================
CREATE TABLE IF NOT EXISTS verb_aspect (
    lemma        TEXT PRIMARY KEY,               -- normalized (lowercase, ё-folded, no stress) dict form
    is_verb      INTEGER NOT NULL DEFAULT 0,     -- 0 = resolved and NOT a verb
    aspect       TEXT,                           -- 'impf' | 'pf' | 'both'
    partner      TEXT,                           -- accented dict form of the other-aspect member
    partner_bare TEXT,                           -- normalized partner, for lookup / linking
    note         TEXT,                           -- short qualifier ('no common perfective', 'colloquial', …)
    checked_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===================================================================
-- Flow reading (app/reading_flow.py) — a fixed-length (default 5-part) LLM piece
-- on a topic the learner picks: a plan with a real arc is drawn first, then each
-- part written to a plan beat. Difficulty auto-tunes part by part from which
-- words the reader taps as unknown, aiming to hold ~98% known-word coverage.
-- When it ends the reader can spawn a sequel (a linked session, with a twist).
-- ===================================================================
CREATE TABLE IF NOT EXISTS reading_flow_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT,                          -- short label
    prompt        TEXT,                          -- the learner's own words, if any
    domain        TEXT,                          -- subject area (proficiency.DOMAINS id)
    rank_est      INTEGER NOT NULL DEFAULT 3500, -- freq rank the reader is estimated to know up to
    plan          TEXT,                          -- JSON {title, hook, beats:[...]} — the arc
    total_parts   INTEGER NOT NULL DEFAULT 5,
    parent_id     INTEGER,                       -- set on a sequel — the session it follows
    chunks        INTEGER NOT NULL DEFAULT 0,
    words_read    INTEGER NOT NULL DEFAULT 0,
    unknown_seen  INTEGER NOT NULL DEFAULT 0,
    summary       TEXT,                          -- running "story so far" for continuity
    status        TEXT NOT NULL DEFAULT 'active', -- active | done | error
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_read_at  TEXT
);

CREATE TABLE IF NOT EXISTS reading_flow_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES reading_flow_sessions(id),
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,                    -- plain (no stress marks) — used for all analysis
    text_accented TEXT,                           -- stress-marked, shown to the reader
    audio_path  TEXT,                             -- local TTS m4a (Silero), built on demand
    rank_est    INTEGER,                         -- the level this chunk was generated for
    n_words     INTEGER NOT NULL DEFAULT 0,
    pred_unknown REAL,                           -- server's pre-check unknown-rate estimate
    read        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS reading_flow_unknown (
    session_id  INTEGER NOT NULL REFERENCES reading_flow_sessions(id),
    lemma       TEXT NOT NULL,
    surface     TEXT,
    sentence    TEXT,
    chunk_seq   INTEGER,
    rank        INTEGER,
    carded      INTEGER NOT NULL DEFAULT 0,
    at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, lemma)
);

-- ===================================================================
-- Conversation partner (app/convo.py) — a spoken, turn-based Russian
-- conversation with an LLM in character (girlfriend's mum at dinner, …). The
-- learner may answer in a Russian/English mix; Whisper transcribes (code-switch
-- aware), the partner replies in natural Russian (ElevenLabs TTS) and silently
-- logs the learner's errors + the things they reached for English on. One row
-- per session; two turns per exchange (learner + partner).
-- ===================================================================
CREATE TABLE IF NOT EXISTS convo_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id  TEXT,
    prompt       TEXT,
    persona      TEXT,                           -- who the partner is
    situation    TEXT,                           -- the setting
    goal         TEXT,                           -- what the learner is trying to do
    level        TEXT NOT NULL DEFAULT 'b1',
    status       TEXT NOT NULL DEFAULT 'active',  -- active | ended | error
    error        TEXT,
    debrief      TEXT,                           -- JSON, set on end
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS convo_turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES convo_sessions(id),
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,                   -- 'learner' | 'partner'
    text        TEXT,                            -- transcript / reply
    translation TEXT,                            -- en gloss of a partner turn
    audio_path  TEXT,                            -- learner recording / partner tts m4a
    meta        TEXT,                            -- JSON {errors:[], gaps:[]} on a learner turn
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, seq)
);

-- ===================================================================
-- Speaking activation (app/activate.py) — turning passive vocabulary active. A
-- silent drill: the app names a target (a verb + its government, or a common
-- word) and a concrete thought to express; the learner forms the Russian in
-- their head or types it, then checks. Items are SRS-scheduled (SM-2-lite).
-- `activate_verbs` is the curriculum (bundled, app/data/activate/); items are
-- introduced from it + the frequency list.
-- ===================================================================
CREATE TABLE IF NOT EXISTS activate_verbs (
    verb        TEXT PRIMARY KEY,               -- infinitive
    rank        INTEGER,
    gloss       TEXT,
    aspect_pair TEXT,
    government   TEXT,                           -- JSON [{gov, role, ex}]
    trap        TEXT,
    hardness    INTEGER NOT NULL DEFAULT 1       -- 0 plain acc … 3 real trap; drives intro order
);

CREATE TABLE IF NOT EXISTS activate_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,                 -- 'verb' | 'word'
    target        TEXT NOT NULL,
    gloss         TEXT,
    introduced_at TEXT NOT NULL DEFAULT (datetime('now')),
    reps          INTEGER NOT NULL DEFAULT 0,
    lapses        INTEGER NOT NULL DEFAULT 0,
    streak        INTEGER NOT NULL DEFAULT 0,
    ease          REAL NOT NULL DEFAULT 2.3,
    interval_d    REAL NOT NULL DEFAULT 0,
    due           TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen     TEXT,
    angles        TEXT,                          -- JSON list of task angles used
    UNIQUE(kind, target)
);

CREATE TABLE IF NOT EXISTS activate_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   INTEGER NOT NULL REFERENCES activate_items(id),
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    rating    INTEGER,
    produced  TEXT,
    category  TEXT,                              -- primary error tag (from the check, or self-reported)
    categories TEXT,                             -- JSON list of every error the learner flagged
    level     TEXT                               -- the working CEFR level at grade time
);

-- ===================================================================
-- Proficiency history (app/proficiency.py) — one row per day, a snapshot of the
-- learner's estimated level so the stats page can graph progress over time.
-- `domains` is JSON {domain_id: {rank_est, comprehension, words_read}}.
-- ===================================================================
CREATE TABLE IF NOT EXISTS proficiency_snapshots (
    day          TEXT PRIMARY KEY,               -- YYYY-MM-DD
    known_words  INTEGER,
    known_rank   INTEGER,
    cefr         TEXT,
    cards_total  INTEGER,
    cards_word   INTEGER,
    cards_mature INTEGER,
    words_read   INTEGER,
    comprehension REAL,
    retention    REAL,
    domains      TEXT,
    ak_coverage  REAL,                            -- share of Anna Karenina's running words known
    active_words INTEGER,                          -- lemmas the learner can actively produce
    speaking_ord REAL,                             -- speaking level on the 0..6 CEFR ladder
    reading_ord  REAL,                             -- reading level on the same ladder (for the gap)
    speaking_cefr TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
