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

- **In-app SRS** (vs Anki) — use `ts-fsrs`, add `srs_*` cols + a `reviews` table,
  keep the `.apkg` export as an escape hatch. Unlocks review-with-video-context.
- **Audio on cards** — yt-dlp bestaudio → ffmpeg slice ±3s → AnkiConnect
  `storeMediaFile` → `[sound:…]`. Turns reading cards into listening cards.
- **Non-YouTube content sources** — movies / dubbed shows (see notes below).
  Constraint: the audio and the subtitles must be the SAME translation, not a
  dub script vs. a separately-made caption file.
- (done) Local RU→EN dict (build_dict.py / WikDict) for instant glosses
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
- Stress marks (`stress` column exists, always blank) — Wiktionary lookup.
- yt-dlp may need cookies for some videos / from datacenter IPs.
