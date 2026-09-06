# Backup & data safety — state of play

_Reviewed 2026-09-05. The learning data (SRS cards + review history, reading
library, proficiency snapshots) is now something the user relies on; this is an
honest look at what protects it and what doesn't._

## What exists today

| Layer | Where | Cadence | Restores via | Off-machine? |
|---|---|---|---|---|
| Working DB | `ru-anki/vocab.db` (internal SSD) | live | — | no |
| Local snapshots | `~/Library/Application Support/ru-anki/backups/vocab-*.db` (24 kept) + `vocab-latest.db` | startup, ~10 min timer, after reviews (debounced 45s) | copy the file back | **no — same SSD, same folder tree** |
| Plain-text export | `~/Library/Application Support/ru-anki/data-git/*.ndjson`, committed + pushed to `github.com/overtonch/ru-anki-data` (private) | same triggers as snapshots | `python rebuild_db.py <dir>` then `build_stoplist.py` + `build_dict.py` | **yes — GitHub, the only copy that survives the Mac dying** |

`backup.GIT_TABLES` is the source of truth for what's in the export. It now
includes the reading stories/chunks and proficiency history. `srs_cards` +
`srs_reviews` (the irreplaceable part) have always been in.

Not backed up, on purpose (regenerable): `freq` / `stoplist` / `dict_ru`
(build scripts), `subtitle_lines` (re-derived from `videos.raw_subs`),
`app/data/stress.db` (rebuilt from the committed `app/data/stress/*.json.gz`),
media clips + TTS/convo audio under `RU_MEDIA_DIR`.

## Risks, roughly worst first

### 0. THE CODE ITSELF IS NOT BACKED UP
`github.com/overtonch/ru-anki` (the code repo, separate from the `ru-anki-data`
backup repo) was **last committed 2026-09-01**. As of this review there are ~37
untracked files and ~20 modified — the entire in-app SRS, speaking practice,
motion/chunks decks, flow reading, conversation partner, proficiency model, and
this backup work — **none of it committed or pushed anywhere.** If the Mac dies,
`rebuild_db.py` restores the *data* but the *application to run it* is gone back
to September 1. This is the biggest single gap and the cheapest to close:
```sh
cd ~/ru-anki && git add -A && git commit -m "wip" && git push
```
Do this now, and get in the habit — or add a periodic `git add -A && git commit`
in a launchd timer if committing-as-you-go isn't going to happen.

### 1. The data export is a single off-site copy — and the restore was never tested
`rebuild_db.py` is a **cold** restore: it recreates the schema and reloads
NDJSON. Every time a column is added to a backed-up table, that script and the
`GIT_TABLES` query can silently drift out of sync, and you'd only find out when
you actually need it. **Mitigated 2026-09-05:** `tests/test_backup_restore.py`
now runs the full export→rebuild round-trip in `check.sh` and asserts row counts
+ that every `GIT_TABLES` query still parses. Keep that green.
Still open: if the GitHub account is locked/deleted, or a bad push force-clobbers
history, that copy is gone. There is no second remote.

### 2. No Time Machine, no iCloud, no second local disk
`tmutil` reports **no destinations configured**. The `.gitignore` comment about
"backed up separately to iCloud" is **stale** — iCloud was abandoned (its sync
wedges and blocks writes; see the note in `backup.py`). So the local snapshots
protect only against "I discarded everything in the app" / "the server trashed
the DB" — **not against SSD failure or losing the machine.** For those, the
GitHub export is the *only* line of defence (see #1).

### 3. Backup failures are silent
`_git_backup` is best-effort. If the SSH key expires, GitHub auth breaks, or the
network is down for a week, pushes fail and the only signal is a line in
`~/Library/Logs/ru-anki.log` and `git.last_ok:false` in `/health`. There is no
alert. `RU_HEARTBEAT_URL` in the plist is **empty**, so there's no dead-man's
switch either.

### 4. Everything lives in one folder on one machine
Working DB, 24 local snapshots, and the local git working copy are all under
`~/…/ru-anki/` and `~/Library/Application Support/ru-anki/`. A stray
`rm -rf`, a disk-full event, or filesystem corruption can take all three at once
(GitHub still has whatever was last pushed).

### 5. The Mac only backs up while it's awake and serving
Snapshots/pushes run inside the server process. `caffeinate` keeps the machine
awake *while the server runs*, and launchd `KeepAlive` restarts a crashed server
— but a wedged process that's alive-but-not-working would stop backing up
without tripping either. (#3 would catch this if a heartbeat existed.)

### 6. Minor
- `.tmp` files from snapshots killed mid-`VACUUM INTO` used to accumulate; now
  swept at the start of each snapshot.
- `data-git` repo has ~4k loose objects, never gc'd (~110 MB). Harmless; a
  `git -C … gc` occasionally would keep it tidy.
- Export NDJSON on GitHub is plaintext (private repo, but readable by anyone with
  repo access / GitHub staff). The content is personal example sentences and
  vocab — low but non-zero sensitivity.

## Recommended fixes (see TODO.md — "Backup hardening")

**Do first (cheap, high value):**
1. **Heartbeat.** Make a healthchecks.io check (period ~1h, grace ~1d), put the
   URL in `RU_HEARTBEAT_URL`, and ping it from `_git_backup` on a successful
   push. Email when backups stop.
2. **Second git remote.** Add a GitLab or Codeberg mirror; `_git_backup` does
   `git push --quiet` then `git push --quiet mirror` (or `git remote set-url
   --add --push origin <both>`). One line of protection against losing the
   GitHub account.
3. **Turn on Time Machine** to any external / network disk. `~/Library/
   Application Support/ru-anki` is already TM-included, so this instantly gives a
   versioned second physical copy of every snapshot + the git working copy.

**Do when home:**
4. **Off-site object copy** of `vocab-latest.db` + the NDJSON dir to Backblaze
   B2 / S3 / rsync.net via `restic` or `rclone` on a `launchd` timer (hourly).
   `restic` gives encryption + dedup + retention for ~free. This is the
   real "the house burned down" copy.
5. **Surface backup health in the app.** `/backup/status` already returns
   last-push time + ok flag; show it on the stats/settings screen with a red
   state when the last successful off-site backup is >24h old.
6. **Disk-space guard** in `maybe_snapshot`: if free space < ~1 GB, skip and
   flag (a full disk currently just logs a caught exception).
7. `pmset autorestart 1` + resolve the FileVault/auto-login tradeoff (already in
   TODO.md under reliability) so a power blip doesn't strand the machine — a Mac
   that won't boot is a Mac that isn't backing up.

## If you ever need to restore

```sh
# from the GitHub export (Mac died):
git clone git@github.com:overtonch/ru-anki-data.git /tmp/ru-data
cd ru-anki && python rebuild_db.py /tmp/ru-data vocab.db
python build_stoplist.py vocab.db
python build_dict.py --db vocab.db
# stress.db rebuilds itself on first run

# from a local snapshot (app trashed the DB):
cp ~/Library/Application\ Support/ru-anki/backups/vocab-latest.db ru-anki/vocab.db
```
