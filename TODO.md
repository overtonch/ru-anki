# ru-anki — things to do

## Set up before relying on it remotely (details: `deploy/RELIABILITY.md`)

- [x] Tailscale **Serve** — `https://angelicas-imac.tail0916c1.ts.net` → :8000
      (persists across reboot). **Use this URL on the phone, not the IP.**
- [x] launchd agent installed (auto-start + auto-restart + caffeinate)
- [ ] **Create a private GitHub repo `ru-anki-data`** (empty, no README) at
      https://github.com/new — the `data-git` remote is already pointed at
      `git@github.com:overtonch/ru-anki-data.git`; the next backup pushes on its
      own once the repo exists. Then confirm `/health` → `backup.git.last_ok: true`.
- [ ] `sudo pmset -a autorestart 1 disksleep 0`  (needs your password; currently
      autorestart=0, disksleep=10)
- [ ] System Settings → Users & Groups → **auto-login** (unattended reboot recovery)
- [ ] Re-add the home-screen app from the **https://** URL (the old icon points at
      `http://100.x` — offline video / SW / PWA only work on the https origin)
- [ ] Leave the Mac plugged in, lid open
- [ ] Know the recovery path: `python rebuild_db.py ~/ru-anki-data`

## Decided but not built

- **In-app SRS** (vs Anki) — use `ts-fsrs`, add `srs_*` cols + a `reviews` table,
  keep the `.apkg` export as an escape hatch. Unlocks review-with-video-context.
- **Audio on cards** — yt-dlp bestaudio → ffmpeg slice ±3s → AnkiConnect
  `storeMediaFile` → `[sound:…]`. Turns reading cards into listening cards.
- **Audio-only download mode** — also gives background playback (screen off) and
  true offline listening. Lighter than caching video.
- (done) Offline video + audio, OPFS + <video>, Media Session — needs device testing

## Rough edges / smaller

- `resolved_words` can drift from actual Anki state (deleting a deck orphans it).
  A reconcile pass would help.
- `EXTRACT_STATUS` is in-memory — a restart mid-extraction looks stuck.
- Stress marks (`stress` column exists, always blank) — Wiktionary lookup.
- yt-dlp may need cookies for some videos / from datacenter IPs.
