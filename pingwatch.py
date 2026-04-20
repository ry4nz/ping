#!/usr/bin/env python3
"""pingwatch: ping a host, expose status as HTTP, alarm in browser if down >= 5 min."""
import http.server
import json
import platform
import re
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

DEFAULT_HOST = "192.168.50.180"
PORT = 8765
PING_INTERVAL = 15  # seconds between pings
HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,253}$")

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"

state = {
    "host": DEFAULT_HOST,
    "reachable": None,
    "last_success": None,
    "last_check": None,
    "consecutive_failures": 0,
    "started_at": time.time(),
}
state_lock = threading.Lock()
# Signals the pinger to break out of its sleep when host changes.
host_changed = threading.Event()


def _ping_argv(host):
    if IS_WINDOWS:
        # Windows ping: -n count, -w timeout per reply in ms.
        return ["ping", "-n", "1", "-w", "2000", host]
    if IS_MACOS:
        # BSD ping: -c count, -W per-reply timeout in ms, -t overall deadline in s.
        return ["ping", "-c", "1", "-W", "2000", "-t", "3", host]
    # Linux iputils ping: -c count, -W per-reply timeout in seconds.
    return ["ping", "-c", "1", "-W", "2", host]


def ping_once(host):
    kwargs = {"capture_output": True, "timeout": 5}
    if IS_WINDOWS:
        # Hide the transient console window that would otherwise flash.
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        r = subprocess.run(_ping_argv(host), **kwargs)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    if IS_WINDOWS:
        # Windows ping can exit 0 even when unreachable (e.g., a gateway
        # returns "Destination host unreachable"). A real echo reply always
        # contains "TTL=" in the output — that's the reliable signal.
        out = (r.stdout or b"") + (r.stderr or b"")
        return b"TTL=" in out or b"ttl=" in out
    return True


def pinger():
    while True:
        with state_lock:
            host = state["host"]
        ok = ping_once(host)
        now = time.time()
        with state_lock:
            # Only record results if host wasn't swapped mid-ping.
            if state["host"] == host:
                state["last_check"] = now
                state["reachable"] = ok
                if ok:
                    state["last_success"] = now
                    state["consecutive_failures"] = 0
                else:
                    state["consecutive_failures"] += 1
        # Sleep, but wake early if the host changed.
        host_changed.wait(timeout=PING_INTERVAL)
        host_changed.clear()


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>pingwatch</title>
<style>
  html, body { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
    margin: 0; padding: 48px;
    background: #111; color: #eee;
    transition: background 0.3s;
  }
  body.ok    { background: #0b3d23; }
  body.down  { background: #5a1a1a; }
  body.alarm { animation: flash 0.55s infinite alternate; }
  @keyframes flash { from { background: #b10000; } to { background: #2a0000; } }

  h1 { font-size: 42px; margin: 0 0 8px; font-weight: 600; }
  .sub { opacity: 0.7; margin-bottom: 24px; }
  .status {
    font-size: 140px; font-weight: 800; letter-spacing: -3px;
    margin: 24px 0; line-height: 1;
  }
  .meta { font-size: 18px; opacity: 0.9; line-height: 1.7; }
  .meta span { font-variant-numeric: tabular-nums; }
  button {
    font: inherit; font-size: 16px; padding: 10px 18px;
    cursor: pointer; background: #fff; color: #111;
    border: 0; border-radius: 8px;
  }
  button:disabled { opacity: 0.55; cursor: default; }
  .config {
    display: flex; gap: 10px; align-items: center;
    margin: 20px 0 4px; flex-wrap: wrap;
  }
  .config input[type=text] {
    font: inherit; font-size: 16px; padding: 10px 12px;
    background: rgba(255,255,255,0.12); color: #fff;
    border: 1px solid rgba(255,255,255,0.3); border-radius: 8px;
    min-width: 240px; font-variant-numeric: tabular-nums;
  }
  .config input[type=text]:focus { outline: 2px solid #fff; outline-offset: 1px; }
  .config .note { font-size: 13px; opacity: 0.7; }
  .config .err { color: #ffb4b4; font-size: 14px; }
  .actions { margin-top: 28px; display: flex; gap: 12px; flex-wrap: wrap; }
</style>
</head>
<body>
  <h1>pingwatch — <span id="host">…</span></h1>
  <div class="sub">alarms after 5 min of consecutive failures</div>
  <form class="config" id="config_form" autocomplete="off">
    <label for="host_input">Target:</label>
    <input type="text" id="host_input" name="host" placeholder="192.168.50.180" spellcheck="false" autocapitalize="off">
    <button type="submit">Set</button>
    <span class="note">IP or hostname; resets counters</span>
    <span class="err" id="config_err"></span>
  </form>
  <div class="status" id="status">…</div>
  <div class="meta">
    Last successful ping: <span id="last_success">—</span><br>
    Consecutive failures: <span id="failures">—</span><br>
    Last checked: <span id="last_check">—</span><br>
    <span id="down_duration"></span>
  </div>
  <div class="actions">
    <button id="enable">Enable alarm + desktop notifications</button>
  </div>

<script>
const ALARM_THRESHOLD_SEC = 5 * 60;
let audioCtx = null;
let alarmTimer = null;
let notified = false;
let enabled = false;

function fmt(ts) {
  if (!ts) return "never";
  return new Date(ts * 1000).toLocaleTimeString();
}

function beep() {
  if (!audioCtx) return;
  const o = audioCtx.createOscillator();
  const g = audioCtx.createGain();
  o.type = "square";
  o.frequency.value = 880;
  g.gain.setValueAtTime(0.0001, audioCtx.currentTime);
  g.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + 0.02);
  g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.28);
  o.connect(g).connect(audioCtx.destination);
  o.start();
  o.stop(audioCtx.currentTime + 0.3);
}

function startAlarm() {
  if (!enabled || alarmTimer) return;
  beep();
  alarmTimer = setInterval(beep, 800);
}

function stopAlarm() {
  if (alarmTimer) { clearInterval(alarmTimer); alarmTimer = null; }
  notified = false;
}

document.getElementById("enable").onclick = async () => {
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    // Prime audio context (browsers require a user gesture).
    const o = audioCtx.createOscillator();
    o.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + 0.001);
  } catch (e) {}
  if ("Notification" in window && Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch (e) {}
  }
  enabled = true;
  const btn = document.getElementById("enable");
  btn.textContent = "Alarm armed ✓";
  btn.disabled = true;
};

const hostInput = document.getElementById("host_input");
const configErr = document.getElementById("config_err");
let hostInputDirty = false;
hostInput.addEventListener("input", () => { hostInputDirty = true; configErr.textContent = ""; });

document.getElementById("config_form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const val = hostInput.value.trim();
  if (!val) { configErr.textContent = "required"; return; }
  try {
    const r = await fetch("/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: val }),
    });
    if (!r.ok) {
      const msg = await r.text();
      configErr.textContent = msg || ("error " + r.status);
      return;
    }
    hostInputDirty = false;
    configErr.textContent = "";
    notified = false;
    poll();
  } catch (err) {
    configErr.textContent = String(err);
  }
});

async function poll() {
  try {
    const r = await fetch("/status", { cache: "no-store" });
    const s = await r.json();
    document.getElementById("host").textContent = s.host;
    if (!hostInputDirty && document.activeElement !== hostInput) {
      hostInput.value = s.host;
    }
    document.getElementById("last_success").textContent = fmt(s.last_success);
    document.getElementById("failures").textContent = s.consecutive_failures;
    document.getElementById("last_check").textContent = fmt(s.last_check);

    const now = Date.now() / 1000;
    const downFor = s.last_success ? (now - s.last_success) : (now - s.started_at);

    if (s.reachable) {
      document.getElementById("status").textContent = "UP";
      document.body.className = "ok";
      document.getElementById("down_duration").textContent = "";
      stopAlarm();
    } else {
      document.getElementById("status").textContent = "DOWN";
      const shouldAlarm = downFor >= ALARM_THRESHOLD_SEC;
      document.body.className = shouldAlarm ? "down alarm" : "down";
      document.getElementById("down_duration").textContent =
        "Down for " + Math.floor(downFor) + "s";
      if (shouldAlarm) {
        startAlarm();
        if (!notified && "Notification" in window && Notification.permission === "granted") {
          try {
            new Notification("pingwatch: " + s.host + " unreachable", {
              body: "No response for " + Math.floor(downFor) + "s",
              requireInteraction: true,
            });
          } catch (e) {}
          notified = true;
        }
        document.title = "⚠ DOWN — " + s.host;
      } else {
        document.title = "pingwatch — " + s.host;
      }
    }
  } catch (e) {
    document.getElementById("status").textContent = "?";
    document.body.className = "";
  }
}

setInterval(poll, 3000);
poll();
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # silence access log

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/status":
            with state_lock:
                body = json.dumps(state).encode("utf-8")
            self._send(body, "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/config":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 4096:
            self.send_error(400, "empty or oversized body")
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
            new_host = str(payload.get("host", "")).strip()
        except Exception:
            self.send_error(400, "invalid JSON")
            return
        if not HOST_RE.match(new_host):
            self.send_error(400, "invalid host (letters/digits/._:- only)")
            return
        now = time.time()
        with state_lock:
            state["host"] = new_host
            state["reachable"] = None
            state["last_success"] = None
            state["last_check"] = None
            state["consecutive_failures"] = 0
            state["started_at"] = now
            body = json.dumps(state).encode("utf-8")
        host_changed.set()
        self._send(body, "application/json")


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    t = threading.Thread(target=pinger, daemon=True)
    t.start()
    addr = ("127.0.0.1", PORT)
    with ThreadedServer(addr, Handler) as httpd:
        url = f"http://127.0.0.1:{PORT}"
        print(f"pingwatch: watching {DEFAULT_HOST}, open {url}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nbye", flush=True)
            sys.exit(0)


if __name__ == "__main__":
    main()
