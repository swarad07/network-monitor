#!/usr/bin/env bash
# Show whether services are loaded, recent activity, current session.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

echo "== launchd =="
loaded="$(launchctl list 2>/dev/null | awk '/com\.swarad\.netmonitor/ {print}')"
if [[ -z "$loaded" ]]; then
    echo "  (no netmonitor jobs loaded)"
else
    printf "  %-10s %-8s %s\n" "PID" "EXIT" "LABEL"
    echo "$loaded" | awk '{printf "  %-10s %-8s %s\n", $1, $2, $3}'
fi

echo
echo "== process =="
if pgrep -fl "monitor.py" > /dev/null 2>&1; then
    pgrep -fl "monitor.py" | sed 's/^/  /'
else
    echo "  (monitor.py not running)"
fi

echo
echo "== recent activity =="
if [[ ! -f "$NM_DB" ]]; then
    echo "  DB not found: $NM_DB"
    exit 0
fi

sqlite3 "$NM_DB" <<SQL
.headers off
.mode list
SELECT '  last probe: ' || datetime(MAX(ts), 'unixepoch', 'localtime')
       || '  (' || (strftime('%s','now') - MAX(ts)) || 's ago)'
FROM probes;

SELECT '  probes last 60s: ' || COUNT(*)
FROM probes WHERE ts > strftime('%s','now') - 60;
SQL

echo
echo "== current session =="
sqlite3 "$NM_DB" <<'SQL'
.mode list
.headers off
SELECT
  '  id:       ' || id || char(10) ||
  '  label:    ' || label || char(10) ||
  '  city:     ' || COALESCE(city,'—') || ', ' || COALESCE(region,'—') || ', ' || COALESCE(country,'—') || char(10) ||
  '  isp:      ' || COALESCE(isp,'—') || char(10) ||
  '  asn:      ' || COALESCE(asn,'—') || char(10) ||
  '  public_ip:' || COALESCE(public_ip,'—') || char(10) ||
  '  gateway:  ' || COALESCE(gateway_ip,'—') || char(10) ||
  '  isp_edge: ' || COALESCE(isp_edge_ip,'—') || char(10) ||
  '  ssid:     ' || COALESCE(ssid,'(wired)') || char(10) ||
  '  started:  ' || datetime(start_ts,'unixepoch','localtime')
FROM sessions ORDER BY start_ts DESC LIMIT 1;
SQL

echo
echo "== last 5 lines of monitor.log =="
if [[ -f "$NM_DIR/monitor.log" ]]; then
    tail -5 "$NM_DIR/monitor.log" | sed 's/^/  /'
else
    echo "  (no log yet)"
fi
