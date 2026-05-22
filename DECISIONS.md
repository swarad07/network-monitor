# Design decisions

This file captures the *why* behind non-obvious choices. The *what* is in the
code; this is for the next time someone (or some Claude) wonders "why is it
done this way and not the other way?"

## Probing

**Why ping at three distinct layers (LAN / ISP edge / WAN) instead of one?**
A single end-to-end ping tells you something failed but not *who* is at fault.
Pinging the gateway, the first non-LAN hop, and an internet target lets us
attribute every outage:

- LAN fails → router, cable, Wi-Fi.
- LAN ok, ISP edge fails → ISP last-mile (the fault we want to prove).
- LAN + ISP edge ok, WAN fails → ISP upstream / peering.

This is the whole point of the tool: removing the "must be your router" excuse
during ISP support calls.

**Why ICMP ping and not TCP / HTTP?**
ICMP is the cheapest probe that most network gear answers. We use the system
`ping` binary (not raw sockets) so no root is needed. Some ISP routers
de-prioritize or drop ICMP — when that happens to the ISP-edge hop, we'd see
constant red there even if service is fine. Mitigation: traceroute is re-run
every 30 minutes to find a hop that does respond. If this becomes a recurring
false positive, fall back to a TCP `nc -z` probe.

**Why 5 seconds between probes?**
Fast enough to catch the kind of 10-15 minute outages the user is troubleshooting
without missing them. Slow enough that overhead is invisible (~1 KB/s of network,
~3 MB/day of disk). One probe every minute would miss flaps; one per second
would 12× the data with no extra signal.

**Why ping in parallel (ThreadPoolExecutor)?**
A serial pass would take up to `targets × ping_timeout` seconds (~8s for 4
targets), which would skew the interval. Parallel keeps the cycle close to its
nominal 5s.

## Discovery & sessions

**Why a `sessions` table?**
Network changes (moving, ISP swap, switching Wi-Fi) invalidate the gateway IP,
the ISP-edge hop, the public IP, and the labels. We can't just keep one
configuration — the *same* gateway IP `192.168.0.1` exists everywhere. A
session row pins a contiguous run of probes to a specific (gateway MAC, SSID,
ISP, city) tuple, so historical data stays correctly attributed when shown
later.

**Why `(gateway_ip, gateway_mac, ssid)` as the change fingerprint?**
- IP alone is unreliable: most LANs use `192.168.x.x`, so the same IP appears
  at different ISPs.
- MAC alone misses Wi-Fi network changes where the same router is in range.
- Together they catch every realistic change cheaply. Recomputed every 60s.

**Why `ip-api.com` for geolocation?**
- Free tier needs no API key.
- Returns ISP, AS, city, region, country in one call.
- Used ≤ once per session, not per probe, so rate limits are a non-issue.
- HTTP-only on the free tier — acceptable since the only thing on the wire is
  our public IP, which the destination already sees.
- If privacy matters more than convenience later: switch to `ipinfo.io` (HTTPS,
  needs a free token) or skip geolocation entirely (the ISP name from the AS
  lookup is enough for our purposes).

**Why traceroute for ISP-edge discovery, not a hardcoded "next hop"?**
We don't know what the user's network looks like — pure-routed, CGNAT
(`100.64/10`), bridged modem with public IP on the WAN interface, etc.
Traceroute is the universal answer: "first hop on the path that isn't on my
LAN." Treating CGNAT as ISP-side is intentional — it *is* ISP infrastructure.

## Storage

**Why SQLite, not Postgres / InfluxDB / Prometheus?**
- Zero install, single file, ships with macOS.
- Workload is tiny: ~70k inserts/day, queries hit indexed timestamp ranges.
- WAL mode allows the dashboard to read while the daemon writes without
  blocking either.
- A time-series DB would be overkill and would add a service to keep alive.

**Why 8-week retention?**
- Enough history to show an ISP a multi-week pattern.
- Bounds disk at ~170 MB worst case — small enough to back up, big enough to
  carry useful context.
- A single integer constant (`RETENTION_DAYS` in `cleanup.py`) — easy to
  change if requirements shift.

**Why a separate `cleanup.py` job, not inline in the monitor?**
- Single responsibility: the monitor's hot loop stays simple.
- launchd handles the schedule (`StartCalendarInterval` at 03:30 daily) — no
  in-process timer to reason about.
- Cleanup can be invoked manually (`./nm cleanup`) and tested in isolation.
- If cleanup ever blocks on something, it doesn't take down probing.

**Why don't we VACUUM automatically after each cleanup?**
- VACUUM rewrites the whole file and requires an exclusive lock. While the
  monitor is running every 5s, this is racy — we'd hit `SQLITE_BUSY` regularly.
- Instead, the cleanup runs `PRAGMA wal_checkpoint(TRUNCATE)` to bound the WAL.
- SQLite reuses free pages internally; the main file stabilizes in steady state.
- `./nm vacuum` is provided for the rare case the user wants to reclaim space
  (it stops the daemon, vacuums, restarts).

**Why isn't the DB file recreated when we change schema?**
- Existing data is valuable (especially the outage history for the ISP call).
- `init_db` in `monitor.py` detects old schemas and renames the legacy table
  rather than dropping it (`probes_legacy_v1`). New schema is created beside it.

## Orchestration

**Why launchd, not cron?**
- Apple's deprecated cron on macOS in favor of launchd.
- launchd handles crash restart (`KeepAlive`), wake-from-sleep, and per-job log
  files natively.
- `StartCalendarInterval` survives reboots without `@reboot` hacks.

**Why templated plists (`.plist.template`)?**
- The plist needs absolute paths for the Python interpreter and the project
  directory. Hardcoding made the original setup non-portable (it only worked
  from the original author's checkout path).
- Templates use `{{NETMONITOR_DIR}}` and `{{PYTHON_BIN}}` placeholders; the
  install script sed-substitutes the real values from the running shell.
- Side benefit: the rendered plist in `~/Library/LaunchAgents` is the "real"
  copy; the template in the repo never has user-specific paths.

**Why is the dashboard not installed by default?**
- The monitor + cleanup are essential for collecting data; the dashboard is a
  viewer. Some users may prefer to query the DB directly or build their own
  view, so we don't force port 8080 to be claimed permanently.
- Opt in with `./nm install --with-dashboard` when you want it always-on.

**Why a single `nm` dispatcher instead of a Makefile?**
- Make is excellent for build graphs, awkward for "run this script" verbs.
- A shell dispatcher is two-line per command, discoverable via `./nm help`, and
  has no implicit phony-target / dependency rules to surprise the reader.
- The actual logic lives in `bin/*.sh`, each invokable standalone — `nm` is
  just sugar.

## Frontend dependencies

**Plotly is vendored at `static/plotly.min.js`, not loaded from a CDN.**
Originally the dashboard pulled `plotly.min.js` from `cdn.plot.ly`. The user's
home ISP (Ssky Conneect in Roha) silently rewrites that request to an
ISP-served block page — `HTTP 200, ~274 bytes` of HTML that the browser tried
to execute as JavaScript and then failed silently, leaving the chart
containers blank. Other CDNs (unpkg, jsdelivr) returned the real 4.5 MB file
fine, but relying on *any* CDN means a future block can re-break the
dashboard for someone who didn't change anything.

Bundling the file solves it permanently and is gitignored to keep the repo
small. `./nm setup` downloads it from unpkg → jsdelivr fallback, with a
sanity check on the byte count to refuse the ISP block-page payload.

## Things deliberately not done

- **No alerting / notifications.** This is a measurement tool, not a pager.
  Adding it would mean choosing thresholds, dedupe windows, channels — out of
  scope.
- **No multi-host coordination.** One machine, one DB. Cross-machine
  aggregation would need a real time-series stack.
- **No PDF / report export.** The dashboard screenshots are the report. Adding
  PDF generation pulls in heavy deps (weasyprint, etc.) for marginal benefit.
- **No Linux port.** A few macOS-specific calls (`route -n get default`,
  `ipconfig getsummary`, `pmset`). Easy to port (`ip route`, `iw dev`) when
  needed — not now.
