# shellcheck shell=bash
# Sourced by every bin/ script. Resolves paths and shared helpers.

set -euo pipefail

NM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NM_DB="$NM_DIR/netmonitor.db"
NM_LAUNCHD_DIR="$HOME/Library/LaunchAgents"

# Services we manage. Format: short-name:label
NM_SERVICES=(
    "monitor:com.swarad.netmonitor"
    "cleanup:com.swarad.netmonitor.cleanup"
    "dashboard:com.swarad.netmonitor.dashboard"
)

nm_label_for() {
    local short="$1"
    for s in "${NM_SERVICES[@]}"; do
        if [[ "${s%%:*}" == "$short" ]]; then echo "${s##*:}"; return; fi
    done
    return 1
}

nm_plist_path() {
    local label="$1"
    echo "$NM_LAUNCHD_DIR/$label.plist"
}

nm_template_path() {
    local label="$1"
    echo "$NM_DIR/launchd/$label.plist.template"
}

nm_python_bin() {
    python3 -c 'import sys; print(sys.executable)'
}
