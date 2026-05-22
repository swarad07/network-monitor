#!/usr/bin/env bash
# Restart one or all netmonitor launchd services.
# Usage: reload [monitor|cleanup|dashboard|all]   (default: monitor)

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

target="${1:-monitor}"

reload_one() {
    local label="$1"
    local plist
    plist="$(nm_plist_path "$label")"
    if [[ ! -f "$plist" ]]; then
        echo "  $label: not installed (no plist at $plist)"
        return
    fi
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    echo "  reloaded: $label"
}

if [[ "$target" == "all" ]]; then
    for s in "${NM_SERVICES[@]}"; do
        reload_one "${s##*:}"
    done
else
    label="$(nm_label_for "$target")" || { echo "unknown service: $target"; exit 2; }
    reload_one "$label"
fi

sleep 1
echo
launchctl list | awk 'NR==1 || /com\.swarad\.netmonitor/'
