# ru-anki — things to do

## Set up before relying on it remotely (details: `deploy/RELIABILITY.md`)

- [x] Tailscale **Serve** — `https://angelicas-imac.tail0916c1.ts.net` → :8000
      (persists across reboot). **Use this URL on the phone, not the IP.**
- [x] launchd agent installed (auto-start + auto-restart + caffeinate)
- [x] `ru-anki-data` private repo created; off-machine backup pushing
      (`/health` → `backup.git.last_ok: true`)
- [ ] `sudo pmset -a autorestart 1 disksleep 0`  (any admin terminal, one time;
      currently autorestart=0, disksleep=10)
- [ ] Re-add the home-screen app from `https://angelicas-imac.tail0916c1.ts.net/`
      (old icon points at `http://100.x` — offline video / SW / PWA need https)
- [ ] Heartbeat alerts: make a healthchecks.io check, put its URL in the plist's
      `RU_HEARTBEAT_URL`, reload the agent
- [ ] Remote Mac recovery (no auto-login): needs FileVault OFF + Remote Login +
      Screen Sharing + Tailscale "run unattended". FileVault is currently ON, so
      today a full reboot = down until you're physically at the Mac.
- [ ] Leave the Mac plugged in, lid open
- [ ] Know the recovery path: `python rebuild_db.py ~/ru-anki-data`

## Decided but not built

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
