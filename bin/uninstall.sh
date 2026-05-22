#!/usr/bin/env bash
# Unload + remove launchd jobs. Keeps the DB and logs untouched.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

for s in "${NM_SERVICES[@]}"; do
    label="${s##*:}"
    plist="$(nm_plist_path "$label")"
    if [[ -f "$plist" ]]; then
        launchctl unload "$plist" 2>/dev/null || true
        rm -f "$plist"
        echo "removed: $label"
    fi
done

echo
echo "Data preserved at:"
echo "  DB:   $NM_DB"
echo "  Logs: $NM_DIR/*.log"
