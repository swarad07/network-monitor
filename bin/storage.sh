#!/usr/bin/env bash
# Show DB size, row counts, retention info, oldest/newest probes.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if [[ ! -f "$NM_DB" ]]; then
    echo "DB not found: $NM_DB"
    exit 1
fi

RETENTION_DAYS=56  # keep in sync with cleanup.py

echo "== files =="
for f in "$NM_DB" "$NM_DB-wal" "$NM_DB-shm"; do
    if [[ -f "$f" ]]; then
        size=$(du -h "$f" | cut -f1)
        bytes=$(stat -f%z "$f")
        printf "  %-32s %8s  (%s bytes)\n" "$(basename "$f")" "$size" "$bytes"
    fi
done

echo
echo "== totals =="
sqlite3 "$NM_DB" <<'SQL'
.headers on
.mode column
SELECT
    (SELECT COUNT(*) FROM probes)   AS probes,
    (SELECT COUNT(*) FROM sessions) AS sessions,
    datetime((SELECT MIN(ts) FROM probes),'unixepoch','localtime') AS oldest,
    datetime((SELECT MAX(ts) FROM probes),'unixepoch','localtime') AS newest;
SQL

echo
echo "== per-layer =="
sqlite3 "$NM_DB" <<'SQL'
.headers on
.mode column
SELECT layer,
       COUNT(*) AS probes,
       SUM(success) AS ok,
       printf("%.3f%%", 100.0 * SUM(success) / COUNT(*)) AS uptime
FROM probes GROUP BY layer ORDER BY layer;
SQL

echo
echo "== sessions (recent) =="
sqlite3 "$NM_DB" <<'SQL'
.headers on
.mode column
.width 4 36 19 19
SELECT id, substr(label,1,36) AS label,
       datetime(start_ts,'unixepoch','localtime') AS started,
       COALESCE(datetime(end_ts,'unixepoch','localtime'),'(current)') AS ended
FROM sessions ORDER BY start_ts DESC LIMIT 10;
SQL

echo
echo "== retention =="
echo "  policy: keep last ${RETENTION_DAYS} days (8 weeks)"
CUTOFF=$(date -v-${RETENTION_DAYS}d +%s)
ELIGIBLE=$(sqlite3 "$NM_DB" "SELECT COUNT(*) FROM probes WHERE ts < $CUTOFF")
echo "  cutoff timestamp: $CUTOFF ($(date -r $CUTOFF '+%Y-%m-%d %H:%M:%S'))"
echo "  rows eligible for next cleanup: $ELIGIBLE"

if [[ -f "$NM_DIR/cleanup.log" ]]; then
    echo
    echo "  last cleanup runs:"
    tail -5 "$NM_DIR/cleanup.log" | sed 's/^/    /'
fi
