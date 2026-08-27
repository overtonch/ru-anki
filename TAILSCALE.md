# Phase 6 — remote access via Tailscale

Goal: reach the exact same app from your phone's browser when you're away from
home, over HTTPS, with no port-forwarding and no cloud host.

You said Tailscale is downloaded. Finish it like this.

## 1. On the Mac

1. Open the Tailscale app, sign in. Menu-bar icon → should show "Connected".
2. Enable the CLI helper (one-time):
   `sudo ln -s /Applications/Tailscale.app/Contents/MacOS/Tailscale /usr/local/bin/tailscale`
3. Find this machine's name / IP:
   `tailscale status`  — note the `100.x.y.z` and the MagicDNS name
   (e.g. `charlies-mbp.tail1234.ts.net`).
4. Serve the app over HTTPS on the tailnet (leave the server running on :8000):
   ```sh
   tailscale serve --bg 8000
   ```
   This publishes `https://<magicdns-name>/` → `localhost:8000`, with a real
   cert, only visible to devices on your tailnet. `tailscale serve status`
   shows the mapping; `tailscale serve --https=443 off` removes it.
   *Do not use `tailscale funnel`* — that exposes it to the public internet;
   we want tailnet-only.

## 2. On the phone

1. Install Tailscale from the App Store, sign in with the same account.
2. Toggle the VPN on. In the admin console (login.tailscale.com) both devices
   should be listed.
3. Visit `https://<magicdns-name>/` in Safari. Add to Home Screen for an
   app-like launch.

## 3. Keep the Mac reachable

- System Settings → Battery / Lock Screen: set "Prevent automatic sleeping
  when the display is off" (or `caffeinate -s` while you're mining).
- The `claude` login and Anki both need to stay running on the Mac.
- Phase 7 will add a launchd job so `run.sh` survives reboots; for now start
  it by hand.

## Checklist — done when

- [ ] `tailscale status` on the Mac lists the phone
- [ ] `https://<magicdns-name>/health` returns JSON from the phone on cellular
- [ ] fetch + extract + review a video end-to-end from the phone, off wifi
