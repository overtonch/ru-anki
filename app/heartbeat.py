"""Dead-man's-switch heartbeat.

Pings an external URL on a schedule. If this server crashes, hangs, or the whole
Mac goes down (reboot, power blip, network drop), the pings stop and the
monitoring service alerts you — no inbound connectivity required.

Works with any "ping this URL every N minutes or alert me" service:
  - Healthchecks.io   (free; email + phone push via their app / Pushover / ntfy)
  - Better Stack heartbeats, Cronitor, UptimeRobot heartbeat, etc.

Config (env, set in deploy/com.ru-anki.server.plist):
  RU_HEARTBEAT_URL        the check / ping URL. Unset => heartbeat disabled.
  RU_HEARTBEAT_INTERVAL   seconds between pings (default 300; min 30).

When the local self-check fails we hit "<url>/fail" (Healthchecks convention)
so you're told the box is up but unhealthy, versus silence = box is gone.
"""
import os
import threading
import time
import urllib.request

import store

URL = os.environ.get("RU_HEARTBEAT_URL", "").strip()
INTERVAL = max(30, int(os.environ.get("RU_HEARTBEAT_INTERVAL", "300")))
_STATE = {"last_ok": None, "last_ping_iso": None, "last_error": None, "pings": 0}


def _self_check():
    c = store.connect()
    try:
        c.execute("SELECT 1").fetchone()
    finally:
        c.close()


def _ping(suffix=""):
    req = urllib.request.Request(
        URL + suffix, headers={"User-Agent": "ru-anki-heartbeat/1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def _loop():
    while True:
        healthy = True
        try:
            _self_check()
        except Exception as e:  # noqa: BLE001
            healthy = False
            _STATE["last_error"] = str(e)[:200]
        try:
            _ping("" if healthy else "/fail")
            _STATE["pings"] += 1
        except Exception as e:  # noqa: BLE001
            _STATE["last_error"] = f"ping failed: {str(e)[:180]}"
        _STATE["last_ok"] = healthy
        _STATE["last_ping_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if healthy:
            _STATE["last_error"] = None
        time.sleep(INTERVAL)


def start():
    if not URL:
        return
    threading.Thread(target=_loop, daemon=True, name="heartbeat").start()


def status():
    return {"configured": bool(URL), "interval": INTERVAL, **_STATE}
