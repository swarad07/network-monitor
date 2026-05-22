#!/usr/bin/env bash
# Reclaim disk space by rewriting the DB. Pauses the monitor daemon briefly
# (VACUUM holds an exclusive lock). Use this manually after a large cleanup,
# not as part of the daily flow.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if [[ ! -f "$NM_DB" ]]; then
    echo "DB not found: $NM_DB"; exit 1
fi

monitor_plist="$(nm_plist_path "$(nm_label_for monitor)")"
was_loaded=0
if launchctl list | grep -q "com.swarad.netmonitor "; then
    was_loaded=1
    echo "Pausing monitor daemon..."
    launchctl unload "$monitor_plist" 2>/dev/null || true
    # Give it a second to release the WAL.
    sleep 2
fi

before=$(stat -f%z "$NM_DB")
echo "Vacuuming (size before: $before bytes)..."
sqlite3 "$NM_DB" "VACUUM;"
after=$(stat -f%z "$NM_DB")
echo "Done. Size after: $after bytes (reclaimed $((before - after)) bytes)"

if (( was_loaded )); then
    echo "Resuming monitor daemon..."
    launchctl load "$monitor_plist"
fi
