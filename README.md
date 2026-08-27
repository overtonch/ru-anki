# ru-anki — YouTube → Russian vocab → Anki

Paste a YouTube link, get Russian vocab candidates pulled from the subtitles,
review them with a tap, and confirmed ones become Anki recognition cards.

## Running

```sh
./run.sh                     # uvicorn on 0.0.0.0:8000
```

Open <http://localhost:8000/> (or `http://<machine>:8000/` on the LAN).

Requirements, already set up in `.venv`: `fastapi`, `uvicorn`, `httpx`,
`yt-dlp`, `genanki`, `pymorphy3`. Plus the `claude` CLI on `PATH` (headless
extraction uses your Pro/Max login — no API key) and, for card creation,
desktop **Anki open with the AnkiConnect add-on** (code `2055492159`).

## Pieces

| file | job |
|---|---|
| `app/main.py`   | FastAPI endpoints |
| `app/store.py`  | SQLite (source of truth); reuses `db.py` for norm/lemma/bold |
| `app/ytdlp.py`  | subtitle fetch + VTT cleaning (`subs.py`) |
| `app/llm.py`    | headless `claude -p` wrapper (extraction + live translation) |
| `app/anki.py`   | AnkiConnect client |
| `app/backup.py` | consistent DB snapshots to iCloud Drive |
| `app/static/index.html` | mobile web UI — one page: video list + per-video detail (progress / search / review) |
| `schema.sql`, `schema_v2.sql` | DB schema (v2 is additive, runs on startup) |

## UI

One page. Paste a link → it fetches subtitles and starts extraction, opening
that video's detail view. The **video list** groups by channel ("Most recent"
first) and each card shows live state: a progress bar while extracting,
"✓ N to review" once done. Tap a video for its **detail view**: live extraction
progress (server-sent events), a "look up a word" search box over the
transcript, and the **Review** cards (sentence with the target bolded +
translation, Make card / Discard). Candidates stream in per chunk, so you can
start reviewing before extraction finishes.

## Speed

Extraction of a 20-min video takes **~9s**. Every `claude -p` call runs with
extended thinking off (`MAX_THINKING_TOKENS=0` — this was the whole bottleneck),
a stripped system prompt (`--system-prompt`, no Claude Code scaffold), no tools,
and a terse `SPAN|TRANSLATION|HH:MM:SS` output format; the sentence for each card
is rebuilt from the indexed transcript (`store.sentence_for`). Chunks run 8-wide.

Knobs: `RU_EXTRACT_MODEL` (default `haiku`), `RU_EXTRACT_WORKERS` (default 8),
`RU_EXTRACT_THINKING` (default `0`; set e.g. `4000` to re-enable thinking),
`CHUNK_LINES` in `app/llm.py`.

Legacy (pre-server) CLI scripts still present: `fetch_subs.py`, `extract.py`
(SDK version, superseded by `app/llm.py`), `db.py` (still used as a library).

## API

```
GET  /health
POST /videos                     {url}                     -> fetch subs, index lines
GET  /videos
GET  /videos/{id}
POST /videos/{id}/extract        ?model=sonnet             -> background extraction
GET  /videos/{id}/extract                                  -> poll status
GET  /videos/{id}/candidates     ?status=pending
POST /candidates/{id}/decision   {decision: yes|no}        -> yes: addNote + sync
GET  /videos/{id}/search         ?q=                       -> local substring, no LLM
POST /videos/{id}/make-card      {subtitle_line_id, span}  -> translate + addNote
```

## Notes / constraints

- Batch extraction chunks the transcript (~170 lines/call); a 20-min video
  takes a few minutes. It's a background task — trigger it, review later.
- The 13k-lemma frequency stoplist is applied *after* extraction in
  `store.add_candidates`, not sent in the prompt. Only `resolved_words` (words
  you've already decided) go into the prompt.
- Anki sync isn't push — a new card reaches AnkiMobile on its next sync.
- The machine must stay awake and reachable for remote triggering.

## Backups

`vocab.db` in this dir is the working database — it persists across restarts
and reboots on its own. On top of that, the server writes crash-safe snapshots
to a synced folder:

- **Where:** `~/Library/Mobile Documents/com~apple~CloudDocs/ru-anki-backup/`
  (iCloud Drive), override with `RU_BACKUP_DIR`.
- **What:** `vocab-latest.db` + timestamped `vocab-YYYYMMDD-HHMMSS.db` (last 24,
  `RU_BACKUP_KEEP`) — each a consistent `VACUUM INTO` copy — plus
  `candidates-latest.ndjson` / `resolved_words-latest.ndjson` /
  `videos-latest.ndjson` (plain text, readable without SQLite) and
  `manifest.json` (counts + timestamp).
- **When:** on startup, after every extraction, after every review decision /
  live card (debounced to ~1/min, `RU_BACKUP_MIN_INTERVAL`), and every 10 min
  (`RU_BACKUP_INTERVAL`). So at most ~1 min of taps is ever at risk.
- **Status:** `GET /backup`; force one with `POST /backup`.
- **Restore:** stop the server, then `python restore_backup.py`
  (`--list` to choose a specific snapshot). It backs up the current
  `vocab.db` to `vocab.db.pre-restore` first.

Running SQLite directly on an iCloud-synced file risks corruption, so the
working DB stays local and only the snapshots sync.

## Offline (train mode)

Before you lose signal: open a video, **Save for offline** (detail view). That
caches its full transcript in the browser (IndexedDB) and a service worker
caches the app shell. Then, with no internet:

- the page still loads; the saved video's transcript search runs **locally**
- tapping a result and making a card **queues it** (header shows "N cards to
  upload")

When you're back online the queue **auto-flushes** — each card is translated and
pushed to Anki in the background (`POST /cards/flush`). Desktop Anki must be
running and reachable (Tailscale) at that point.

## Remote access (Tailscale)

See `TAILSCALE.md`.
