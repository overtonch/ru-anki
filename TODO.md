# ru-anki — things to do

## Set up before relying on it remotely (details: `deploy/RELIABILITY.md`)

- [ ] Create a **private** GitHub repo `ru-anki-data` (empty), then:
      `cd ~/ru-anki && sh deploy/install.sh git@github.com:overtonch/ru-anki-data.git`
      → wires the off-machine backup (until then, backups are local only)
- [ ] `sudo pmset -a autorestart 1 disksleep 0`  (survive a power blip)
- [ ] System Settings → Users & Groups → **auto-login** (unattended reboot recovery)
- [ ] Enable Tailscale **Serve** (https://login.tailscale.com/f/serve), then
      `tailscale serve --bg 8000`
- [ ] Leave the Mac plugged in, lid open
- [ ] Confirm `GET /health` → `backup.git.last_ok: true`
- [ ] Know the recovery path: `python rebuild_db.py ~/ru-anki-data`

## Decided but not built

- **In-app SRS** (vs Anki) — use `ts-fsrs`, add `srs_*` cols + a `reviews` table,
  keep the `.apkg` export as an escape hatch. Unlocks review-with-video-context.
- **Audio on cards** — yt-dlp bestaudio → ffmpeg slice ±3s → AnkiConnect
  `storeMediaFile` → `[sound:…]`. Turns reading cards into listening cards.
- **Audio-only download mode** — also gives background playback (screen off) and
  true offline listening. Lighter than caching video.
- **Offline video** (Phase B) — 360p download + SW range-request handling.

## Rough edges / smaller

- `resolved_words` can drift from actual Anki state (deleting a deck orphans it).
  A reconcile pass would help.
- `EXTRACT_STATUS` is in-memory — a restart mid-extraction looks stuck.
- Stress marks (`stress` column exists, always blank) — Wiktionary lookup.
- yt-dlp may need cookies for some videos / from datacenter IPs.
