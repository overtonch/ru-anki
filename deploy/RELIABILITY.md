# Running ru-anki reliably (so it survives you being on vacation)

Two goals: **the server stays up**, and **your data is never lost**.

## Quick setup

```sh
cd ~/ru-anki
sh deploy/install.sh git@github.com:overtonch/ru-anki-data.git
```

(Create the private `ru-anki-data` repo on GitHub first — empty, no README.)

Then the manual bits below.

---

## 1. Server stays up

`deploy/install.sh` installs a **LaunchAgent** (`~/Library/LaunchAgents/com.ru-anki.server.plist`):

- **RunAtLoad** — starts when you log in
- **KeepAlive** — if it crashes, launchd restarts it within ~10s (tested)
- wrapped in `caffeinate -is` — the Mac won't idle-sleep while the server runs
- logs to `~/Library/Logs/ru-anki.log`

Control it:
```sh
launchctl kickstart -k gui/$(id -u)/com.ru-anki.server   # restart now
launchctl unload ~/Library/LaunchAgents/com.ru-anki.server.plist   # stop
```

### Survive a power blip / reboot (do these by hand)

```sh
sudo pmset -a autorestart 1     # power comes back -> Mac boots
sudo pmset -a disksleep 0
```

For an **unattended reboot to fully recover**, the Mac must log in on its own:
**System Settings → Users & Groups → Automatically log in as → [you]**.
(Security trade-off: physical access = logged in. On a home Mac usually fine.)

### Don't let it sleep

`caffeinate` stops *idle* sleep, but **closing the lid still sleeps it** unless
it's on power with an external display (clamshell), or:
```sh
sudo pmset -c disablesleep 1    # never sleep on AC — optional, runs a bit warmer
```
Simplest: **leave it plugged in, lid open.**

### Remote access

Enable Tailscale **Serve** (one click: <https://login.tailscale.com/f/serve>), then:
```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 8000
```
→ `https://<your-mac>.<tailnet>.ts.net/` from anywhere on your tailnet.

---

## 2. Data is never lost

### Layer 1 — local snapshots
`VACUUM INTO` snapshots + text exports in
`~/Library/Application Support/ru-anki/backups/` (last 24). Written on startup,
after each extraction, ~1/min during review, every 10 min. Covers "server
crashed" / "I discarded the wrong thing".

Restore: stop the server, `python restore_backup.py`.

> Note: this used to point at iCloud Drive. **Don't** — iCloud sync can wedge and
> block writes indefinitely (it did). Keep it local.

### Layer 2 — off-machine git backup (the vacation insurance)
Set `RU_BACKUP_GIT_DIR` (the plist already does) to a git repo with a **private
remote**. Every snapshot exports `videos/candidates/resolved_words.ndjson` —
plain text, includes the transcripts — and `git commit && git push`.

These three files **fully rebuild the database**:
```sh
git clone git@github.com:overtonch/ru-anki-data.git
cd ~/ru-anki
python rebuild_db.py ~/ru-anki-data
# then re-derive subtitle_lines + stoplist (see rebuild_db.py docstring)
```

Verified end-to-end: git export → `rebuild_db.py` → working DB with all data.

**Check it's working:** `GET /health` → `.backup.git.last_ok` should be `true`,
or `git -C ~/Library/Application\ Support/ru-anki/data-git log` shows recent
commits. If pushes fail, `ssh -T git@github.com` should greet you by name; if it
prompts for a passphrase, add the key to the keychain:
```sh
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```
and in `~/.ssh/config`: `Host github.com` / `  UseKeychain yes` / `  AddKeysToKeychain yes`.

### Your Anki cards
Once a card is created and synced to AnkiWeb it's safe regardless of this app.
While away, if desktop Anki is closed, new cards just **queue on your phone** and
upload when you're home / Anki is open again — nothing is lost.

---

## Pre-vacation checklist

- [ ] `sh deploy/install.sh <data-repo-url>` run, `/health` returns ok
- [ ] `sudo pmset -a autorestart 1 disksleep 0`
- [ ] auto-login enabled
- [ ] Mac plugged in, lid open
- [ ] `tailscale serve --bg 8000` running; open the `.ts.net` URL from your phone
- [ ] `/health` → `backup.git.last_ok: true`
- [ ] desktop Anki open + synced (so cards flow while you're away, if the Mac stays up)
- [ ] you know: `python rebuild_db.py` restores everything from the `ru-anki-data` repo
