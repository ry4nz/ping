# pingwatch

Tiny zero-dependency ping monitor with a browser UI. Pings a host every 15s, serves a status page on `localhost:8765`, and after 5 minutes of consecutive failures: flashes the page red, plays a looping alarm tone, and fires a desktop notification.

## Run

macOS / Linux:
```sh
python3 pingwatch.py
```

Windows (PowerShell or cmd):
```
py pingwatch.py
```

Then open <http://127.0.0.1:8765> and click **Enable alarm + desktop notifications** once (browsers require a user gesture before audio/notifications).

The target host is configurable from the page — default is `192.168.50.180`. Changing it resets the failure counter and the alarm threshold.

## Notes

- Python 3, stdlib only. Uses the system `ping` binary (flags selected per OS).
- Browser can't send ICMP itself, hence the local helper.
- Port, ping interval, and default host are constants at the top of `pingwatch.py`.
