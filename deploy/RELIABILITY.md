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

Run once, in **any** terminal with your admin account (not tied to this session —
it writes system config that persists across reboots):

```sh
sudo pmset -a autorestart 1     # power/kernel-panic -> Mac boots back up
sudo pmset -a disksleep 0
```

**After a reboot, what actually comes back depends on FileVault + login:**

| | FileVault ON (current) | FileVault OFF |
|---|---|---|
| **before anyone logs in** | disk locked — **no network at all**, no SSH, no Tailscale, no Screen Sharing. Dead until someone types the password at the physical Mac. | boots to the login screen **with** network: SSH, Screen Sharing and Tailscale daemon come up |
| **auto-login on** | logs itself in → server + Anki start unattended | same |
| **auto-login off** | — | you Screen-Share to the login window from your phone, type your password, server + Anki start |

So for "get alerted + fix it remotely without auto-login" you need
**FileVault OFF** + Remote Login + Screen Sharing (see *Remote access* below).
With FileVault ON and auto-login off, a true reboot needs physical access —
mitigate by making reboots rare (UPS, `autorestart 1`, stable power) and by the
heartbeat alert so you at least know.

A **server-only crash** (OS still up, you still logged in) is already handled by
`KeepAlive`; if that ever wedges, SSH in over Tailscale and
`launchctl kickstart -k gui/$(id -u)/com.ru-anki.server`.

### Don't let it sleep

`caffeinate` stops *idle* sleep, but **closing the lid still sleeps it** unless
it's on power with an external display (clamshell), or:
```sh
sudo pmset -c disablesleep 1    # never sleep on AC — optional, runs a bit warmer
```
Simplest: **leave it plugged in, lid open.**

### Remote access

**The app** — Tailscale **Serve** is on:
`https://angelicas-imac.tail0916c1.ts.net/` from anywhere on your tailnet
(persists across reboot). Use this URL, not `http://100.x` — it's the only one
that's a secure context (offline video / service worker / PWA need that).

**The Mac itself** (to recover it remotely). All reachable over Tailscale once
the machine is unlocked/up:
```sh
sudo systemsetup -setremotelogin on          # SSH  (System Settings > General > Sharing > Remote Login)
```
Then also turn on **Screen Sharing** (System Settings → General → Sharing →
Screen Sharing) — this is what lets you log in at the login window from an
iPad/Mac after a reboot. On iPhone use a VNC client, or Screen Sharing from any Mac.
Set Tailscale to **Run unattended** (Tailscale menu bar → Preferences) so the
tunnel is up before you log in.

> None of this helps while **FileVault** has the disk locked after a cold boot —
> the network isn't up yet. FileVault OFF is the price of remote reboot recovery.

### Get alerted when it goes down (heartbeat / dead-man's switch)

The server pings an external URL every 5 min. If it — or the whole Mac — stops,
the pings stop and you get emailed / phone-pushed. No inbound access needed.

1. Make a free check at <https://healthchecks.io> (period 5 min, grace 5 min).
   Add the email + push/Pushover/ntfy integrations you want.
2. Put its ping URL in `~/Library/LaunchAgents/com.ru-anki.server.plist` →
   `RU_HEARTBEAT_URL`, then
   `launchctl unload … && launchctl load …/com.ru-anki.server.plist`.
3. `GET /health` → `.heartbeat.configured: true`, `.last_ok: true`.

A healthy-but-degraded server (DB unreadable) pings `…/fail` so you can tell
"box is up but broken" from "box is gone".

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

- [x] `/health` returns ok; LaunchAgent installed
- [ ] `sudo pmset -a autorestart 1 disksleep 0`
- [ ] Mac plugged in, lid open
- [x] Tailscale Serve up — `https://angelicas-imac.tail0916c1.ts.net/`
- [x] `/health` → `backup.git.last_ok: true`
- [ ] heartbeat: `RU_HEARTBEAT_URL` set, `/health` → `heartbeat.last_ok: true`
- [ ] decide FileVault: **ON** (reboot needs physical access) vs **OFF** +
      Remote Login + Screen Sharing + Tailscale-unattended (reboot recoverable remotely)
- [ ] if keeping auto-login OFF and FileVault ON: know that a reboot means the
      app is down until you're back at the Mac
- [ ] desktop Anki open + synced (so cards flow while you're away, if the Mac stays up)
- [ ] you know: `python rebuild_db.py` restores everything from the `ru-anki-data` repo
