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

## LLM latency audit (asked 2026-09-07)

The app now leans on headless `claude -p` for a lot. Done so far: reading-session
creation is async + the plan call is haiku; `_warm` pool covers translate /
card-meanings / word-family / accent.
- [ ] Route more call sites through `llm._warm` (needs a STABLE system prompt —
      move per-request vars like rank/cefr into the user message). Candidates:
      `reading_flow_chunk`, `activate_prompt`, `activate_check`, `stress_resolve`.
- [ ] `prewarm()` the pools it'll actually use, on startup.
- [ ] Haiku where precision is cheap: `reading_topics`, `activate_check` (already
      focused), maybe `stress_resolve`. Keep sonnet for card content + prose.
- [ ] Make `POST /reading/sessions/{sid}/next` async too (same polling pattern).

## UI polish — follow-ups (2026-09-07 pass did: scroll/overscroll, hidden
scrollbars, min-height:0 on flex bodies, reading review-strip, flow-sheet back
buttons, async reading-gen spinner)
- [ ] Give `#actSheet`, `#convoSheet`, `#genSheet`, `#flowWords` the same
      `.sheet-back` top affordance.
- [ ] Audit remaining full-screen views for a visible back control + working
      `history.back()` (drill/motion/chunk/journal/speech overlays).
- [ ] Spinner: a few `$('#x').innerHTML = SPINNER` sites land the spinner
      top-left; wrap in `.f-load` / `.center-spin` consistently.
- [ ] The stats tab strip: fade the right edge so it reads as scrollable now
      that the scrollbar is hidden.

## TTS

- **Default is now Piper** (`ru_RU-irina-medium`) — local neural, CPU real-time,
  and espeak-ng phonemisation respects the app's U+0301 stress marks so stress is
  right. Models live in `~/Library/Application Support/ru-anki/piper/` (not in the
  repo). Add/replace: `cd` there and
  `python -m piper.download_voices ru_RU-dmitri-medium` (voices: irina/dmitri/
  denis/ruslan, `-medium`). Switch with `RU_TTS_PIPER_VOICE=` in the plist,
  slower with `RU_TTS_PIPER_LENGTH=1.15`. Falls back to Apple `say` then Silero
  if no model is present.
- Apple `say` alternative — for Milena (Enhanced) or a Siri Russian voice:
  System Settings › Accessibility › Spoken Content › Manage Voices › Russian,
  then `RU_TTS_SAY_VOICE=` + a bogus `RU_TTS_PIPER_VOICE` (or delete the models)
  so `say` wins.
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
  - (done 2026-09-06) **CEFR difficulty a1→c2** (`activate.LEVELS` + `_LEVEL_GUIDE`),
    exposed as a slider; **self-adjusting** (`_adapt_level`, 85% rule — see
    LEARNING.md #13) with a manual override + `auto` toggle.
  - (done 2026-09-06) **hardest government first** — `_hardness` scores each verb
    0–4 off its primary pattern (bare instr/gen object = 4, unpredictable prep =
    3, directional в/на = 2, plain acc / trivial verbs = 0); intro order is
    `hardness DESC, rank ASC`. "I know this — skip" retires an item (rating 5).
  - (done 2026-09-06) **endless** — `per_day` 0 = unlimited; `next_item` always
    introduces or serves the soonest-due, never dead-ends.
  - (done 2026-09-06) **diagnostic sentences** — prompt keeps everything around
    the target lexically trivial so a failure implicates the target; complexity
    slider scales *structure* not word rarity.
  - (done 2026-09-06) **granular failure tags** — `_MISS_TAGS`, self-reported on
    a miss, stored in `activate_log.categories`, drive `weak_spots`. Check now
    runs via `/activate/items/{id}/check` (no double-log).
  - (done 2026-09-06) **calibration sweep** — `GET/POST /activate/calibrate`
    (`calibration_batch` / `calibrate`): sweep the not-yet-mastered list
    (verbs hardest-government first), tap the ones you already handle → retired
    as active. The fast way to skip a big passive vocabulary. Button in
    `actSettings`. Adaptive climb also jumps 2 rungs on a perfect streak
    (`_ADAPT_WINDOW` 8). **Bug fixed**: `#actSheet input` had a blanket
    `-webkit-appearance:none` that broke the level slider (no thumb → undraggable)
    and the auto toggle (invisible box) — scoped to `[type=number]`, added a real
    range thumb + `.tgl` switch.
  - (done 2026-09-06) **speaking level ladder** — `speaking_levels.py`: per-CEFR
    productive-vocab bands, speaking `ord` on the same 0..6 scale as reading,
    `gap` tracked in `proficiency_snapshots` (`speaking_ord`/`reading_ord`/
    `speaking_cefr`); rendered on the Level tab (`profSpeaking` + `profDualSpark`).
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

- (done 2026-09-06) **manual next-part + level gate** — flow reading only
  generates the next part on an explicit "continue reading" tap; a 0-tap session
  no longer moves `known_rank` or the session's `rank_est` (skimming ≠
  comprehension). Fiction eases the period vocab in over the parts.
- (done 2026-09-07) **multi-factor new-card priority + backlog triage** — the
  order new cards are introduced is now `srs_cards.priority` (0-100), a weighted
  blend of: SPEAK + DAILY + CULTURE/fiction (LLM `card_priority`), overall
  frequency rank, Anna-Karenina appearance, and recency-of-creation (halflife 12d
  — a card made 3 weeks ago whose context is cold shouldn't outrank a fresh one).
  Weights in `app_settings.new_card_weights` (`GET/POST /srs/new-card-weights`,
  re-derivable without an LLM call via `rescore_priorities_from_meta`).
  `_rank_new_cards` calls `card_priority` daily. Triage: `GET /srs/new-triage`
  (worst-priority first, with the breakdown) + `POST /srs/cards/bulk`
  {ids, suspend|delete}; `#triageView` screen, opened from the `#newReserve`
  home strip.
- (done 2026-09-07) **reading word marks — all forms** — `reading_flow._mark_words`
  lemmatises every surface in the chunks server-side and returns `marks`
  ({lemmas: surf→lemma, tap: [...], card: [...], card_gloss}). Frontend
  `flowMarkClass`/`flowPaintWords`: words tapped this session are HIGHLIGHTED in
  every inflected form; words you already had a card for (untapped) are
  UNDERLINED (green, `.fw.known-card`). Tapping an underlined word peeks at the
  card's meaning without counting it (`flowPeekCard`); "still don't know it"
  converts it to a real tap. Fixes the old strict surface-string matching.
- (done 2026-09-07) **min-interval floor on a pass** — FSRS hands a
  repeatedly-failed card a sub-day stability and then schedules Good AND Easy for
  tomorrow (identical, feels broken, buries you in reviews). `srs._floor_pass`:
  passing a graduated card now always buys `MIN_GOOD_DAYS`=2 / `MIN_EASY_DAYS`=4,
  and nudges stability up to match. Applied in `review()`, `preview()`, and the
  final state of `rebuild_schedule` replays. One-shot floor lifted 84 live cards
  off <1-2d; state-2 cards <1d dropped 76→0.
- (done 2026-09-07) **Study silent mode** — `STUDY_SILENT` (localStorage), 🔊/🔇
  toggle in the study toolbar + a done-screen checkbox. Gates every auto-play
  (`playStudyAudio(manual)`); manual listen/replay buttons still work; disables
  audio-first and next-clip prefetch while on.
- (done 2026-09-07) **FSRS state-reset bug + full honest replay** — ~35 cards
  were reset on Sept 5 to `stability=3.0, difficulty=6.5` (not FSRS values;
  cause unconfirmed — a restore/rebuild artifact) with due/last_review kept,
  wiping their lapse history; a later Good/Easy then inflated them to a 7-11d
  interval they hadn't earned. `srs.rebuild_all_schedules()` now replays EVERY
  reviewed card's log on an honest schedule (collapsing Again-runs within an
  hour = drilling, not repeated failures): a card with the `3.0/6.5` fingerprint
  gets the honest value even if shorter (marked in `app_settings.srs_reset_
  repaired` so it isn't re-touched); every other card can only grow. Live: 114
  cards grew (avg +12d), 24 shrank. `daily_healthcheck` detects the fingerprint.
- (done 2026-09-07) **FSRS early-review bug + healthcheck** — the daily batch
  surfaced every graduated card hours before its `due`; FSRS read that as "no
  time elapsed" and stability never grew, so the whole collection was trapped
  near a 1-day interval (Good and Easy both showed 1d). Fix: `srs._on_schedule_time()`
  scores a review of a card that was due within today's batch window AS IF it
  happened on the due date (review() + preview()); a rating-1/way-ahead review
  still counts as early. `rebuild_schedule()` / `rebuild_all_schedules()` +
  `POST /srs/rebuild-schedules` replay a flattened card's log on an honest
  schedule (repaired ~69 live cards). `srs.daily_healthcheck()` (runs once/day in
  `_maybe_card_audit`, also `POST /srs/healthcheck`) catches state wipes,
  flattened/impossible schedules, and review-load spikes, auto-repairs what it
  can, and stores a report surfaced on the Reviews stats tab. Also: ~7 cards had
  their FSRS state wiped around Sept 1 (`due` forced to `2026-09-01T00:00:00`) —
  cause not pinned (a restore/rebuild artifact); healthcheck now catches that class.
- (done 2026-09-06) **archive + evolving topic pool** — `POST /reading/sessions/
  {sid}/archive`, `recent(archived=)`, `set_archived()` (status 'archived',
  restored to done/active; `next_chunk` won't clobber it). Library has an
  "archived (N)" section; archive buttons in the studybar, end-card, and each
  library row. The subject picker's per-category topics evolve: starting a piece
  from a suggestion retires it (`note_topic_started`) and a bg thread tops the
  category up (`_rotate_one`); `POST /reading/topics/{did}/refresh` +
  `refresh_topics()` swap the whole set ("↻ more ideas" button). Pool in
  `app_settings` `reading_topic_pool` / `reading_topic_used`;
  `llm.reading_topics()` generates. SW v171.
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
