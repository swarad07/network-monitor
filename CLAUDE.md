# CLAUDE.md — notes for AI sessions working on this repo

This is a personal tool that probes the user's home internet at three layers
and renders attributed outages, so the user can show their ISP evidence during
support calls. The user is **Swarad Mokal** (Technical Program Manager at
Axelerant); see his global CLAUDE.md for personal preferences (concise, no
fluff, ask before doing critical/destructive things).

Always read `DECISIONS.md` before changing core behavior — it captures *why*
choices were made and which trade-offs are deliberate.

## What this repo does

1. `monitor.py` — daemon. Pings gateway (LAN), first non-LAN hop (ISP edge),
   and `1.1.1.1` + `8.8.8.8` (WAN) every 5s. Writes every probe to SQLite with
   a layer tag (`lan` / `isp` / `wan`) and a `session_id`. On network changes,
   closes the current session and opens a new one with auto-discovered
   topology + geolocation.
2. `app.py` — Flask dashboard on `:8080`. Heatmap timeline per layer, RTT
   chart, outage log with cause attribution.
3. `cleanup.py` — daily retention job. Keeps last 56 days (8 weeks).
4. `nm` — CLI dispatcher to everything in `bin/`.
5. `launchd/*.plist.template` — rendered by `bin/install.sh` into
   `~/Library/LaunchAgents/`.

## File layout

```
nm                                  CLI entrypoint
monitor.py / app.py / cleanup.py    Python services
requirements.txt                    flask
bin/                                shell helpers, all source bin/_common.sh
launchd/                            plist templates with {{PLACEHOLDERS}}
netmonitor.db                       SQLite store (runtime; do not check in)
backups/                            ./nm backup output (do not check in)
*.log                               launchd-managed stdout/stderr
README.md / DECISIONS.md / this     docs
```

## Schema (current — `monitor.py:init_db`)

```
sessions(id, start_ts, end_ts, gateway_ip, gateway_mac, interface, ssid,
         isp_edge_ip, public_ip, isp, city, region, country, label)
probes(ts, session_id, layer, target, success, rtt_ms)
INDEX probes(ts); probes(session_id); probes(layer)
```

If you change the schema:
- Don't drop the legacy table. `init_db` already renames any old `probes`
  without `session_id` to `probes_legacy_v1`. Mirror that pattern for future
  migrations.
- Update `cleanup.py`, `app.py` API queries, and `bin/storage.sh`.

## How to test changes

```bash
./nm reload                    # restart monitor daemon to pick up code changes
./nm status                    # confirm it came back up
./nm logs monitor              # tail and watch
./nm dashboard                 # open the UI to verify visually
```

For monitor.py changes that touch SQL or schema, also run cleanup once:
```bash
./nm cleanup
./nm storage                   # confirm rows still consistent
```

## Operational gotchas

1. **Stale WAL/SHM files.** If `netmonitor.db` is deleted but `.db-shm`/`.db-wal`
   linger, the next process gets `disk I/O error`. Always
   `rm netmonitor.db netmonitor.db-shm netmonitor.db-wal` together.
2. **Port 8080 conflicts.** A previous `app.py` instance may still bind it.
   `lsof -ti:8080 | xargs kill` before restarting.
3. **launchd cache.** After editing a plist, `launchctl unload` + `launchctl
   load` (or `./nm reload`) is required. Just editing the file does nothing.
4. **pyenv shims in launchd.** launchd doesn't have a shell PATH, so the plist
   must reference an *absolute* python path. `bin/install.sh` resolves it via
   `python3 -c 'import sys; print(sys.executable)'` — uses whatever python3 is
   active at install time.
5. **The user's running daemon.** A `launchd` job is currently live in
   `~/Library/LaunchAgents/com.swarad.netmonitor.plist`. When reorganizing,
   prefer additive changes (new files, renames in repo) over disrupting what
   `launchctl` already loaded. After reorganization, run `./nm install` to
   re-render the plist from the new template path.
6. **Geolocation API.** `ip-api.com` HTTP, no key, ≤1 call per session. Don't
   put it in the per-probe path.

## Conventions

- Bash scripts: `set -euo pipefail`, source `bin/_common.sh`.
- Python: no external deps in `monitor.py` / `cleanup.py` (stdlib only).
  `app.py` uses Flask. Keep it that way unless there's a strong reason.
- Comments: only when the *why* is non-obvious (see DECISIONS.md style).
- Don't add features the user didn't ask for. This repo grew from a 50-line
  pinger; resist scope creep.

## When the user reports an issue

Ask first:
- What does `./nm status` say?
- What's in the last 20 lines of `monitor.log`?
- Did the network change recently (would create a new session row)?

Don't immediately rm the DB or unload launchd jobs — those are destructive and
contain history the user may need for an ISP call.
