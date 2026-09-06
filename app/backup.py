"""Crash-safe backups of vocab.db.

Two layers:
  1. Local `VACUUM INTO` snapshots (fast, safe while the DB is in use) kept in
     Application Support — covers "the server crashed / I discarded everything".
  2. An off-machine git repo of plain-text exports that fully rebuild the DB
     (RU_BACKUP_GIT_DIR) — covers "the Mac died". Verifiable: check `git log`.

(iCloud Drive was the first target but its sync can wedge and block writes
indefinitely — avoid pointing BACKUP_DIR at it.)

Snapshot triggers: on startup, after each extraction, after review decisions
(debounced), and on a timer.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time

import store

DEFAULT_DIR = os.path.expanduser("~/Library/Application Support/ru-anki/backups")
BACKUP_DIR = os.environ.get("RU_BACKUP_DIR", DEFAULT_DIR)
KEEP = int(os.environ.get("RU_BACKUP_KEEP", "24"))      # timestamped .db snapshots
MIN_INTERVAL = int(os.environ.get("RU_BACKUP_MIN_INTERVAL", "45"))  # seconds
TIMER_INTERVAL = int(os.environ.get("RU_BACKUP_INTERVAL", "600"))

# Off-machine, verifiable backup: a git repo of plain-text exports that fully
# rebuild the DB (see rebuild_db.py). Set RU_BACKUP_GIT_DIR to a directory that
# is a git repo with a private remote; snapshots commit + push there.
GIT_DIR = os.environ.get("RU_BACKUP_GIT_DIR", "")

_lock = threading.Lock()
_git_lock = threading.Lock()
_last_snapshot_at = 0.0
_last_db_mtime = 0.0
_last_git_push = 0.0
_last_git_ok = None


def _db_mtime():
    try:
        return os.path.getmtime(store.DB)
    except OSError:
        return 0.0


def _export_ndjson(conn, path, query):
    cols = None
    with open(path, "w", encoding="utf-8") as f:
        for row in conn.execute(query):
            if cols is None:
                cols = row.keys()
            f.write(json.dumps({k: row[k] for k in cols}, ensure_ascii=False) + "\n")


def snapshot(reason="manual"):
    """Write a full consistent snapshot + NDJSON exports. Returns the manifest."""
    global _last_snapshot_at, _last_db_mtime
    with _lock:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        # sweep up .tmp files a previously-killed snapshot left behind
        for f in os.listdir(BACKUP_DIR):
            if f.endswith(".tmp"):
                try:
                    os.remove(os.path.join(BACKUP_DIR, f))
                except OSError:
                    pass
        ts = time.strftime("%Y%m%d-%H%M%S")
        snap = os.path.join(BACKUP_DIR, f"vocab-{ts}.db")
        tmp = snap + ".tmp"

        src = sqlite3.connect(store.DB)
        try:
            src.execute("VACUUM INTO ?", (tmp,))
        finally:
            src.close()
        os.replace(tmp, snap)
        shutil.copy2(snap, os.path.join(BACKUP_DIR, "vocab-latest.db"))

        conn = sqlite3.connect(snap)
        conn.row_factory = sqlite3.Row
        try:
            _export_ndjson(conn, os.path.join(BACKUP_DIR, "candidates-latest.ndjson"),
                           """SELECT c.*, v.title AS video_title, v.url AS video_url
                              FROM candidates c JOIN videos v ON v.id = c.video_id
                              ORDER BY c.id""")
            _export_ndjson(conn, os.path.join(BACKUP_DIR, "resolved_words-latest.ndjson"),
                           "SELECT * FROM resolved_words ORDER BY resolved_at")
            _export_ndjson(conn, os.path.join(BACKUP_DIR, "videos-latest.ndjson"),
                           """SELECT id, url, title, subs_kind, subs_lang, fetched_at,
                              length(raw_subs) AS raw_subs_len FROM videos ORDER BY id""")
            counts = {
                "videos": conn.execute("SELECT count(*) FROM videos").fetchone()[0],
                "candidates": dict(conn.execute(
                    "SELECT status, count(*) FROM candidates GROUP BY status").fetchall()),
                "resolved_words": conn.execute(
                    "SELECT count(*) FROM resolved_words").fetchone()[0],
                "subtitle_lines": conn.execute(
                    "SELECT count(*) FROM subtitle_lines").fetchone()[0],
            }
        finally:
            conn.close()

        # prune old timestamped snapshots (keep the newest KEEP)
        snaps = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("vocab-") and f.endswith(".db")
                       and f != "vocab-latest.db")
        if KEEP > 0:
            for old in snaps[:-KEEP]:
                try:
                    os.remove(os.path.join(BACKUP_DIR, old))
                    snaps.remove(old)
                except OSError:
                    pass

        manifest = {
            "last_snapshot": ts,
            "last_snapshot_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": reason,
            "db_bytes": os.path.getsize(snap),
            "counts": counts,
            "kept_snapshots": len(snaps),
            "dir": BACKUP_DIR,
        }
        with open(os.path.join(BACKUP_DIR, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        _last_snapshot_at = time.time()
        _last_db_mtime = _db_mtime()

    if GIT_DIR:
        threading.Thread(target=_git_backup, args=(reason,), daemon=True).start()
    return manifest


# Every table that holds data you can't cheaply regenerate. Left out on purpose:
# freq / stoplist / dict_ru (rebuilt by the build_*.py scripts), subtitle_lines
# (re-derived from videos.raw_subs), candidate_sentences_cache (memoised LLM
# output), known_lexicon (legacy, folded into resolved_words). Order matters for
# FK-clean reload: parents before children.
GIT_TABLES = (
    ("videos", "SELECT * FROM videos ORDER BY id"),
    ("candidates", "SELECT * FROM candidates ORDER BY id"),
    ("resolved_words", "SELECT * FROM resolved_words ORDER BY resolved_at"),
    ("texts", "SELECT * FROM texts ORDER BY id"),
    ("text_chapters", "SELECT * FROM text_chapters ORDER BY id"),
    ("srs_cards", "SELECT * FROM srs_cards ORDER BY id"),
    ("srs_reviews", "SELECT * FROM srs_reviews ORDER BY id"),
    ("app_settings", "SELECT * FROM app_settings ORDER BY key"),
    ("word_accent", "SELECT * FROM word_accent ORDER BY lemma"),
    ("word_family", "SELECT * FROM word_family ORDER BY lemma"),
    ("word_gloss", "SELECT * FROM word_gloss ORDER BY lemma"),
    ("lyric_notes", "SELECT * FROM lyric_notes ORDER BY video_id, line_index"),
    # grammar-drill history (the graded cards only — the unseen buffer regenerates)
    ("drill_items", "SELECT * FROM drill_items WHERE verdict IS NOT NULL ORDER BY id"),
    ("drill_lapse", "SELECT * FROM drill_lapse ORDER BY id"),
    # verbs-of-motion history (graded cards only — the unseen buffer regenerates)
    ("motion_items", "SELECT * FROM motion_items WHERE verdict IS NOT NULL ORDER BY id"),
    ("motion_lapse", "SELECT * FROM motion_lapse ORDER BY id"),
    # chunk-deck history (graded cards + the per-chunk scaffold state)
    ("chunk_items", "SELECT * FROM chunk_items WHERE verdict IS NOT NULL ORDER BY id"),
    ("chunk_lapse", "SELECT * FROM chunk_lapse ORDER BY id"),
    ("chunk_stage", "SELECT * FROM chunk_stage ORDER BY chunk_id"),
    ("journal_sessions", "SELECT id, duration, level, transcript, status, error, analysis, "
     "created_at FROM journal_sessions ORDER BY id"),
    # speech lab — the text is real content; audio_path/status/voice regenerate
    ("speeches", "SELECT id, title, topic, topic_id, source, ru, en, notes, practice_count, "
     "last_practiced, created_at FROM speeches ORDER BY id"),
    # flow reading — the generated stories ARE content (each is a unique LLM
    # generation the reader may return to), plus the words they didn't know
    ("reading_flow_sessions", "SELECT id, topic, prompt, domain, rank_est, chunks, words_read, "
     "unknown_seen, summary, status, created_at, last_read_at FROM reading_flow_sessions ORDER BY id"),
    ("reading_flow_chunks", "SELECT session_id, seq, text, text_accented, n_words, read, created_at "
     "FROM reading_flow_chunks ORDER BY session_id, seq"),
    ("reading_flow_unknown", "SELECT session_id, lemma, surface, sentence, rank, carded, at "
     "FROM reading_flow_unknown ORDER BY session_id, at"),
    # conversation practice — transcripts + debriefs are real content; audio regenerates
    ("convo_sessions", "SELECT id, scenario_id, prompt, persona, situation, goal, level, "
     "status, debrief, created_at FROM convo_sessions ORDER BY id"),
    ("convo_turns", "SELECT id, session_id, seq, role, text, translation, meta, created_at "
     "FROM convo_turns ORDER BY id"),
    # proficiency history — the level graphs; small and worth keeping
    ("proficiency_snapshots", "SELECT day, known_words, known_rank, cefr, cards_total, "
     "cards_word, cards_mature, words_read, comprehension, retention, domains, created_at "
     "FROM proficiency_snapshots ORDER BY day"),
)


def _git_backup(reason):
    """Export the full DB as text into the git repo and push it. Best-effort."""
    global _last_git_push, _last_git_ok
    with _git_lock:
        try:
            conn = sqlite3.connect(store.DB)
            conn.row_factory = sqlite3.Row
            try:
                for name, query in GIT_TABLES:
                    try:
                        _export_ndjson(conn, os.path.join(GIT_DIR, f"{name}.ndjson"), query)
                    except sqlite3.OperationalError as e:  # table not in an older DB
                        print(f"[backup] skip {name}: {e}")
            finally:
                conn.close()

            def g(*a):
                return subprocess.run(["git", "-C", GIT_DIR, *a],
                                      capture_output=True, text=True, timeout=90)

            g("add", "-A")
            if g("status", "--porcelain").stdout.strip():
                g("commit", "-m", f"backup ({reason}) {time.strftime('%Y-%m-%d %H:%M')}")
            push = g("push", "--quiet")     # also retries a previously-failed push
            _last_git_push = time.time()
            _last_git_ok = push.returncode == 0
            if not _last_git_ok:
                print(f"[backup] git push failed: {push.stderr.strip()[:300]}")
        except Exception as e:  # noqa: BLE001
            _last_git_ok = False
            print(f"[backup] git backup error: {e}")


def maybe_snapshot(reason="auto"):
    """Snapshot only if the DB changed since the last one and we're past the
    debounce interval. Cheap to call often."""
    if _db_mtime() <= _last_db_mtime:
        return None
    if time.time() - _last_snapshot_at < MIN_INTERVAL:
        return None
    try:
        return snapshot(reason)
    except Exception as e:  # noqa: BLE001
        print(f"[backup] snapshot failed: {e}")
        return None


def snapshot_async(reason="auto"):
    """Try to snapshot now; if debounced, schedule one just past the window so a
    lone change can't sit unsaved until the slow timer."""
    def go():
        if maybe_snapshot(reason) is None and _db_mtime() > _last_db_mtime:
            t = threading.Timer(MIN_INTERVAL + 1, maybe_snapshot, args=(reason,))
            t.daemon = True
            t.start()
    threading.Thread(target=go, daemon=True).start()


def start_scheduler():
    def loop():
        while True:
            time.sleep(TIMER_INTERVAL)
            maybe_snapshot("timer")
    threading.Thread(target=loop, daemon=True).start()


def status():
    manifest_path = os.path.join(BACKUP_DIR, "manifest.json")
    m = {}
    if os.path.exists(manifest_path):
        try:
            m = json.load(open(manifest_path, encoding="utf-8"))
        except (OSError, ValueError):
            pass
    snaps = []
    if os.path.isdir(BACKUP_DIR):
        snaps = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("vocab-") and f.endswith(".db"))
    git = None
    if GIT_DIR:
        git = {"dir": GIT_DIR, "last_push_iso":
               time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_last_git_push))
               if _last_git_push else None, "last_ok": _last_git_ok}
    return {"dir": BACKUP_DIR, "exists": os.path.isdir(BACKUP_DIR),
            "snapshots": snaps, "manifest": m, "git": git,
            "pending_changes": _db_mtime() > _last_db_mtime}
