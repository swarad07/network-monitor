# netmonitor

A small always-on tool that probes your network at three layers (LAN gateway,
ISP edge, public internet) and renders attributed outages in a web dashboard.
Built to produce evidence you can show your ISP when they keep telling you
"it's a problem on your side."

- Probes every 5 seconds, 24×7 via `launchd`.
- Auto-discovers your gateway, the first ISP hop, public IP, city, and ISP name.
- Tags every probe with a **session** so data stays attributable when you change
  networks (home → coffee shop → office).
- Retention: keeps 8 weeks, deletes older data automatically.
- All data stays local in `netmonitor.db` (SQLite). The only external call is
  `ip-api.com` for geolocation, once per network change.

## Quick start

```bash
# 1) Install Python dep (Flask for the dashboard)
pip install -r requirements.txt

# 2) Fetch vendored frontend assets (Plotly bundle) — needed because
#    some ISPs block cdn.plot.ly; see DECISIONS.md
./nm setup

# 3) Install the launchd jobs (monitor daemon + daily cleanup at 03:30)
./nm install

# 3) Confirm it's running
./nm status

# 4) Open the dashboard
./nm dashboard          # foreground, Ctrl+C to stop
# or for a permanent dashboard service:
./nm install --with-dashboard
```

Visit `http://localhost:8080` (or `http://netmonitor.swarad:8080` if you added
the hosts entry).

## Common commands

| Command            | What it does                                                 |
| ------------------ | ------------------------------------------------------------ |
| `./nm status`      | Show daemon health, recent activity, current session         |
| `./nm storage`     | DB size, row counts, what's eligible for next cleanup        |
| `./nm cleanup`     | Run retention cleanup now (delete probes > 8 weeks)          |
| `./nm vacuum`      | Reclaim disk after a big cleanup (briefly pauses daemon)     |
| `./nm reload`      | Restart the monitor daemon (pick up code changes)            |
| `./nm logs`        | `tail -F monitor.log monitor.err.log`                        |
| `./nm backup`      | Snapshot DB to `backups/` (safe while daemon writes)         |
| `./nm uninstall`   | Remove launchd jobs (DB and logs are preserved)              |

Each `bin/*.sh` is self-contained — you can also invoke them directly.

## File layout

```
netmonitor/
├── nm                      # CLI dispatcher
├── monitor.py              # daemon: probes LAN/ISP/WAN every 5s -> SQLite
├── app.py                  # Flask dashboard on :8080
├── cleanup.py              # retention enforcement (run by launchd daily)
├── requirements.txt        # flask
├── bin/                    # helper scripts (install, status, storage, ...)
├── launchd/                # plist templates (rendered by bin/install.sh)
├── backups/                # output of ./nm backup
├── netmonitor.db           # SQLite store (created at runtime)
├── *.log                   # stdout/stderr of each launchd job
├── README.md               # you are here
├── DECISIONS.md            # design rationale
└── CLAUDE.md               # notes for future AI sessions on this repo
```

## How to read the dashboard

- **Session card** (top) — current network: city, ISP, gateway, ISP-edge hop.
- **Per-layer status cards** — uptime %, outage count, longest outage for LAN
  (you ↔ router), ISP edge (router ↔ ISP), WAN (ISP ↔ internet).
- **Outage timeline heatmap** — three rows, one per layer. Green = healthy,
  yellow = partial loss, red = full layer outage, gray = no data.
- **WAN latency** — RTT over time. Gaps mean lost packets; baseline rises
  before failures often indicates ISP congestion / buffer bloat.
- **Outage log** — every outage with a **"Likely cause"** attribution:
  - LAN/router (your side)
  - ISP last-mile (ISP fault) — the smoking gun
  - Upstream/peering (ISP transit)
- **Scope dropdown** — "Current network only" (default) vs "All networks in
  window". Use the former when showing data for the connection you're on.

## Keeping it running when the lid closes

Closing the lid sleeps macOS unless you tell it not to. On AC power:

```bash
sudo pmset -c disablesleep 1   # prevent sleep even with lid closed (AC only)
sudo pmset -c disablesleep 0   # revert
```

Without AC power, Apple enforces clamshell sleep — no override.

## Storage and retention

- ~70k probe rows/day ≈ ~3 MB/day. 8 weeks ≈ ~170 MB.
- Cleanup runs daily at 03:30 local time. It deletes probes older than 56 days
  and truncates the WAL but does **not** auto-vacuum (see `DECISIONS.md` for
  why). The DB size plateaus at retention.
- To reclaim free pages after a large delete, run `./nm vacuum` manually.

## Troubleshooting

| Symptom                                          | Fix                                                     |
| ------------------------------------------------ | ------------------------------------------------------- |
| Port 8080 in use                                 | `lsof -ti:8080 \| xargs kill`                           |
| Dashboard shows "stale" for all layers           | Daemon isn't running. `./nm status`, then `./nm reload` |
| `disk I/O error` after deleting the `.db` file   | Stale `.db-shm`/`.db-wal` — `rm netmonitor.db-*`        |
| ISP-edge hop missing (chart row blank)           | First non-LAN hop drops ICMP. Will be auto-rediscovered every 30 min. |
| Geolocation says wrong city                      | `ip-api.com` is approximate. Use the ISP name + public IP instead, both visible in the session card. |

## Uninstall

```bash
./nm uninstall              # removes launchd jobs, keeps data
rm -rf /Users/swarad/Work/netmonitor   # nukes everything
```
