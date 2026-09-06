# ru-anki — things to do

## Set up before relying on it remotely (details: `deploy/RELIABILITY.md`)

- [x] Tailscale **Serve** — `https://angelicas-imac.tail0916c1.ts.net` → :8000
      (persists across reboot). **Use this URL on the phone, not the IP.**
- [x] launchd agent installed (auto-start + auto-restart + caffeinate)
- [x] `ru-anki-data` private repo created; off-machine backup pushing
      (`/health` → `backup.git.last_ok: true`)
- [ ] `sudo pmset -a autorestart 1 disksleep 0`  (any admin terminal, one time;
      currently autorestart=0, disksleep=10) — without this a power blip leaves
      the Mac off until someone powers it on
- [ ] **FileVault + no auto-login = every reboot needs physical presence.** The
      launchd *user* agent can't start until someone logs in, and FileVault
      blocks auto-login. Pick one: (a) FileVault OFF + `sysadminctl` auto-login +
      autorestart so the box fully self-heals, or (b) accept manual recovery, or
      (c) a small UPS for blips. Today = option (b) by default.
- [ ] Heartbeat: `RU_HEARTBEAT_URL` in the plist is still empty → no alert if the
      server/Tailscale goes down. Make a healthchecks.io check (~5 min), paste
      the URL, `launchctl kickstart -k`.
- [ ] Second physical copy of the local snapshots: they live only on the internal
      SSD next to the working DB. `rsync` `~/Library/Application Support/ru-anki/
      backups` to an external drive or rclone target on a cron. (The GitHub
      data-git repo is the off-machine copy but rebuild_db.py is a cold restore.)
- [ ] Re-add the home-screen app from `https://angelicas-imac.tail0916c1.ts.net/`
      (old icon points at `http://100.x` — offline video / SW / PWA need https)
- [ ] Heartbeat alerts: make a healthchecks.io check, put its URL in the plist's
      `RU_HEARTBEAT_URL`, reload the agent
- [ ] Remote Mac recovery (no auto-login): needs FileVault OFF + Remote Login +
      Screen Sharing + Tailscale "run unattended". FileVault is currently ON, so
      today a full reboot = down until you're physically at the Mac.
- [ ] Leave the Mac plugged in, lid open
- [ ] Know the recovery path: `python rebuild_db.py ~/ru-anki-data`

## Backup hardening (full analysis: `deploy/BACKUP.md`)

Today the ONLY thing that survives the Mac dying is the `ru-anki-data` GitHub
repo (cold restore via `rebuild_db.py`). No Time Machine, no iCloud (abandoned),
no second remote, no alert if backups stop. Cards + review history + the reading
library + proficiency graphs all ride on this.

- [ ] **COMMIT + PUSH THE CODE.** `github.com/overtonch/ru-anki` last commit is
      2026-09-01; ~37 untracked + ~20 modified files (all the SRS / speaking /
      reading / convo / proficiency work) exist only on the internal SSD. Cheap
      to fix (`git add -A && git commit && git push`); scariest gap if the Mac
      dies. Consider a launchd timer that auto-commits WIP.

- [x] Restore is verified in CI — `tests/test_backup_restore.py` runs the full
      export→rebuild round-trip in `check.sh` (catches schema drift in
      `backup.GIT_TABLES` / `rebuild_db.TABLES`). Keep green.
- [x] Reading stories/chunks + proficiency snapshots added to the git export
      (were previously not backed up at all).
- [ ] **Heartbeat** — healthchecks.io check, URL in the plist's
      `RU_HEARTBEAT_URL`, ping from `_git_backup` on a successful push. (Also
      listed under reliability above — same check can cover both.)
- [ ] **Second git remote** — GitLab/Codeberg mirror; `_git_backup` pushes to
      both. One line against losing the GitHub account.
- [ ] **Turn on Time Machine** (external or network disk). `~/Library/Application
      Support/ru-anki` is already TM-included → instant versioned 2nd physical
      copy of snapshots + git working copy.
- [ ] **Off-site object copy** — `restic`/`rclone` of `vocab-latest.db` + the
      NDJSON dir to B2 / S3 / rsync.net on a launchd hourly timer. Encrypted,
      dedup'd. The real "house burned down" copy.
- [ ] **Backup health in the UI** — `/backup/status` has last-push + ok flag;
      show it on stats/settings, red when last off-site success >24h old.
- [ ] **Disk-space guard** in `backup.maybe_snapshot` — skip + flag if free
      space < ~1 GB (currently a full disk just logs a caught exception).
- [ ] Occasional `git -C data-git gc` — ~4k loose objects, ~110 MB, never gc'd.

## TTS

- Default voice is now Apple `say` / **Milena (compact)**. For a big quality
  jump: System Settings › Accessibility › Spoken Content › System Voice ›
  Manage Voices › Russian → download **Milena (Enhanced)** or a Siri voice,
  then set `RU_TTS_SAY_VOICE="Milena (Enhanced)"` in the plist and
  `launchctl kickstart -k`. Everything (reading, Speech Lab, convo) picks it up.
- ElevenLabs is **disabled** (2026-09-05, to avoid a metered bill). To re-enable:
  set `RU_TTS_ALLOW_ELEVENLABS=1` **and** `ELEVENLABS_API_KEY=…` in the plist /
  secrets.env. The key alone does nothing without the allow flag.
- Silero fallback stays for non-macOS. To force it: `RU_TTS_SAY_VOICE` to a
  bogus value, or `prefer="silero"`.

## Decided but not built

- **Speaking activation** — `app/activate.py`, the "Activate" mode.
  `LEARNING.md` has the rationale. One mode, `kind`-tagged tracks, `mix` slider.
  - (done 2026-09-06) **verb-government gym** — 220-verb bundled curriculum
    (`app/data/activate/verbs.json.gz`, `build_activate_verbs.py`), personalized
    micro-prompts, SM-2-lite, focused LLM check on typed attempts.
  - (done 2026-09-06) **productive-vocabulary track** — words introduced
    newest-frequent from `freq`, same loop. Active-word count on the Level tab
    (`proficiency_snapshots.active_words`).
  - **track 3 — construction / frame gym** (`kind='frame'`) — curated ~40–60
    high-value structures (если…то, чтобы+inf, то, что…, не только…но и,
    чем…тем, кое-/-нибудь, participial/gerund phrases), each drilled by "express
    X using this structure" across varied contexts. Same scheduler + loop; needs
    a `activate_frames` bundle + `_introduce` branch + a frame prompt in llm.py.
  - later: word "rungs" (frame-fill → own sentence → link to previous word);
    "say it another way" (produce one concept 3 ways); transformation chains
    (timed morphology warm-up).

- (done 2026-09-01) **Card format v2** — `CARDS.md` is the spec. Back = one clean
  bold `translation` + concise `alt_meanings` + a one-clause `sentence` (full
  context kept in `sentence_full`). `meaning_contextual` flags the rare
  in-context primary. `POST /srs/reformat-cards` backfills; `_maybe_reformat_imminent`
  (daily, on queue/stats load) brings the next ~2 days of new cards up to spec
  and re-runs v2 cards that fail `srs.cards_failing_v2`; `_refine_new_card_async`
  refines new cards at creation. `llm.card_meanings` does the batched LLM work.
- (done 2026-09-02) **Reformulation speaking practice** — `app/speak.py` +
  `speak_*` tables + `srs_cards.card_type` ('recognition'|'production'). LLM
  hands you a concrete everyday thought in English (domain-rotated via
  `llm.SPEAK_DOMAINS` — family, restaurants, work, opinions…); you say it in
  Russian (typed or hold-to-record → local Whisper `/speak/transcribe`); the LLM
  returns 3 native reformulations of the ORIGINAL thought (register-labelled) +
  a tiered inline diff (`llm.speaking_feedback`, one big call, ~30s, background
  task). Gaps → production cards (`srs.create_production_card`, learn_score 92,
  bypass the daily recognition budget in `srs.queue`, never orphans). Reviewed
  self-graded front-to-back (English cue → recall + speak Russian, TTS on
  reveal). `#speakView` batch flow (3/5/10), `/speak/stats` mistake-category
  view. Tests: `test_speak.py`. SW v110.
- (done 2026-09-02) **Coloured-word tap/drag fixes** — a tap on a yellow/green
  word with any finger drift was eaten by `attachPlayerSwipe` / the transcript
  drag-select and either did nothing or looked de-highlighted; phrase drag-select
  didn't exist on the fullscreen caption. Fixes: `attachPlayerSwipe` ignores
  pointers that start on `.capw`; taps resolve from the word the finger went DOWN
  on (`attachWordSelect` `end()`), not the drifting click target; `attachWordSelect`
  generalised and wired to `#capOverlay` too (immediate drag, no long-press since
  the caption can't scroll). All screens now paint via `wordClasses()` and route
  via `wordActionKind()` — pinned by `tests/word_render.test.mjs` (in check.sh)
  + `test_api.py::test_{watch,read}_word_flag_states` + `tests/tap_interaction.mjs`
  (browser, run by hand). SW v109.
- (done 2026-09-02) **Catch-up refresher** — `GET /srs/refresher?days=N`. See
  the server-architecture memory.
- (done 2026-09-01) **Song audio sync** — `app/lrcfix.py`: Whisper-transcribe the
  audio, match transcript lines to lyric lines, robust-median the time gaps to
  recover the constant LRC offset, apply via `store.shift_song_timing`
  (subtitle_lines + VTT + card timestamps, `videos.lrc_offset` running total).
  Auto-runs on song ingest; `POST /songs/{id}/fix-audio-sync`; ◀ ▶ nudge +
  "check audio sync" on the song page. Rejects tempo-drift songs (flags for
  swap-source). Backfill: Husky −0.8s, 4 already fine, Triagrutrika flagged.

- (done 2026-08-28) **In-app SRS** — `app/srs.py`, FSRS via `py-fsrs`,
  `srs_cards` + `srs_reviews` + `app_settings`. Study view in the PWA. Audio
  clips + audio-first mode, offline review (idb v2 + `/srs/reviews/flush`),
  stats view. New cards go here; Anki dual-write is a setting, OFF by default.
  `.apkg` export at `/srs/export`. KFP (video 6) backfilled fresh.
  Follow-ups: FSRS param optimisation once there's review history; a real
  settings screen (currently tucked in the study done-screen); audio on Anki
  dual-write cards; pre-cache clips for offline audio; reconcile if an srs
  card's candidate sentence is later edited (card keeps the snapshot).
- **Audio on cards** — yt-dlp bestaudio → ffmpeg slice ±3s → AnkiConnect
  `storeMediaFile` → `[sound:…]`. Turns reading cards into listening cards.
- **Non-YouTube content sources** — movies / dubbed shows (see notes below).
  Constraint: the audio and the subtitles must be the SAME translation, not a
  dub script vs. a separately-made caption file.
- (done) Local RU→EN dict (build_dict.py / WikDict) for instant glosses
- (done 2026-08-29) **Offline SRS review — full, with audio** — `GET /srs/offline?days=2`
  bundles the next 2 days of due cards (each with `due` / `due_now`) + the clip/
  frame URLs. `syncOfflineSRS()` caches the bundle in idb and pre-downloads the
  clips into a `ru-anki-srs-media` Cache (on boot, on Study open, on reconnect,
  every 30 min). SW serves `/videos/*/clip` + `/videos/*/frame` cache-first and
  slices byte ranges for offline `<audio>`. `loadStudyQueue` offline now uses the
  bundle (filtered to actually-due) instead of the last plain queue snapshot.
  Reviews still queue in `srsOut` → `/srs/reviews/flush` on reconnect.
- (done 2026-08-29) **Delete video = choose keep-or-delete cards** — `videos.hidden`
  soft-delete. `DELETE /videos/{id}?cards=keep` archives (row + transcript stay,
  media freed, cards keep video_id/timestamp so jump/clip/occurrences work);
  `?cards=delete` removes video + every SRS card from it. `#vidDelView` shows the
  affected cards before you pick. Archived section on the home screen (restore /
  delete forever). `filter=orphan` + `POST /srs/cards/orphans/delete` +
  `⚠ N with no source` home link for the pre-soft-delete detached cards.
- (done 2026-08-29) **Music / songs** — `app/music.py`. Paste a song link → 🎵
  button → `POST /songs`. Synced lyrics from LRCLIB (lrclib.net, free/keyless),
  fall back to the video's own RU subs, then Whisper. Stored as `kind='song'`
  video → same extraction / cards / word pages. Player: audio + karaoke lyrics
  (`.song-mode`), tap line to seek, 🔁 repeat, 0.75× speed. "Music" home section.
  **Apple Music links**: iTunes Lookup API (keyless) resolves title/artist/art,
  then a yt-dlp `ytsearch` finds the audio on YouTube. `music.apple.com` links
  auto-route to the song flow.
  Follow-ups: A–B loop for drilling a phrase; move LRCLIB-miss subtitle lookup
  off the request path; offline (OPFS) for songs; paste-your-own-lyrics;
  Spotify / Yandex Music links (need the same resolve-to-YouTube step).
  - (done 2026-08-29) **Line-by-line deep read** — 💬 icon on each lyric line in
    song mode → `POST /songs/{id}/explain {index}` → `llm.explain_lyric` (whole
    song as context) → {translation, gist, notes[]} covering undertones / what
    they're bragging about / double entendres / idioms / references. Memoised in
    `lyric_notes`. Bottom sheet `#lyricSheet`, "✎ card from line".
- (done) Reading feature — EPUB/txt/paste import, scroll reader, tap-to-card
- (done) Non-YouTube video via yt-dlp (VK/RuTube/Dzen) + plain VTT/SRT subs
- (done) Offline video + audio, OPFS + <video>, Media Session — needs device testing

## Reading feature — follow-ups

- **Offline card queue** — reading cards are online-only right now (toast if
  offline). Wire them through the same idb `queue` + `/cards/flush` path as
  video cards (needs a `text_id` variant in FlushItem / _make_one_card).
- **Better EPUB chapters** — some EPUBs pack several chapters per spine file, so
  the TOC shows one entry for a block. Use the EPUB nav doc / NCX `<navMap>` to
  split, and honour `#fragment` anchors in the spine hrefs.
- **Vocab extraction on texts** — a per-chapter "Extract vocab" button → the
  same chunked LLM pass → a review/swipe list, like videos.
- **`build_dict.py` run on the server** — `dict_ru` is populated locally; make
  sure it's re-run after any `rebuild_db.py`.

## Non-YouTube video — limitations

- Many RuTube / VK videos have **no subtitles at all** → the app can't build a
  transcript, so they're unusable. Native Russian shows are hit-or-miss.
- yt-dlp can be blocked by geo-restriction / DRM on some RuTube/VK content.
- Non-YouTube videos must be **downloaded to watch** (no embeddable player) —
  fine for movies, needs the https origin (OPFS).
- Whisper re-transcription (TODO above) would cover the "no subs" case.

## Content sources beyond YouTube (movies / TV / dubs)

Goal: watch Russian dubs of Western films (Kung Fu Panda etc.) and Russian
shows, learning from matched audio + subtitles.

**The matching problem is real.** For a *dubbed* film, the Russian you hear is
the dubbing studio's script; the Russian subtitle file is usually a *separate*
translation (often closer to the English original, different word choices,
different sentence splits). Forced-narrative subs or "subtitles for the deaf/HoH"
(СДХ / SDH) are the ones transcribed from the actual dub audio — those match.
Plain "Russian subtitles" usually don't.

Options, roughly best-to-worst for this use case:
- **Russian originals with official subs** (kinopoisk/okko/etc. have Russian
  captions for Russian shows that are near-verbatim) — audio & subs match by
  construction. Best learning material anyway.
- **yt-dlp already supports many non-YouTube sites** (`yt-dlp --list-extractors`
  → vk, ok.ru, rutube, dzen). VK/RuTube host lots of dubbed content with
  embedded or sidecar subs; quality of the match varies, check per-video.
- **Local files** — if the user has an .mkv with an embedded Russian audio track
  + embedded Russian subtitle track ripped from the same release, those often
  match (same distributor). Add an "upload a file" path: ffmpeg to extract the
  sub track + transcode audio, reuse the whole pipeline. No scraping.
- **Whisper-transcribe the dub audio** instead of trusting a subtitle file —
  guarantees audio/text match, costs GPU/CPU time per video. `faster-whisper`
  large-v3 on the Mac. This sidesteps the matching problem entirely and also
  fixes YouTube auto-caption errors. Heaviest but most correct.

Recommendation: (1) add a local-file import path (ffmpeg extract, no scraping),
(2) add optional Whisper re-transcription as the "make it actually match" mode.

## Rough edges / smaller

- `resolved_words` can drift from actual Anki state (deleting a deck orphans it).
  A reconcile pass would help.
- `EXTRACT_STATUS` is in-memory — a restart mid-extraction looks stuck.
- (done) Stress marks + ё — `word_accent` table, LLM-generated at card time /
  on word-page open, shown on the card back + word page (never the transcript).
- (evaluated, shelved) whisper.cpp + CoreML backend — built & benchmarked
  2026-08-28, came out ~6× RT vs mlx-turbo's ~8-9× on this base M1. Wired as
  opt-in (`RU_WHISPER_BACKEND=whispercpp`). See `deploy/WHISPERCPP.md`. Revisit
  on a bigger-ANE Mac.
- yt-dlp may need cookies for some videos / from datacenter IPs.
