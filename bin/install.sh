#!/usr/bin/env bash
# Install launchd jobs from templates. Idempotent.
#
# By default installs monitor + cleanup. Pass --with-dashboard to also install
# the dashboard as a background service (port 8080).

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

WITH_DASHBOARD=0
for arg in "$@"; do
    case "$arg" in
        --with-dashboard) WITH_DASHBOARD=1 ;;
        *) echo "unknown arg: $arg"; exit 2 ;;
    esac
done

PYTHON_BIN="$(nm_python_bin)"
echo "netmonitor dir: $NM_DIR"
echo "python bin:     $PYTHON_BIN"
echo "launchd dir:    $NM_LAUNCHD_DIR"

mkdir -p "$NM_LAUNCHD_DIR"

install_one() {
    local label="$1"
    local template
    template="$(nm_template_path "$label")"
    local dest
    dest="$(nm_plist_path "$label")"

    if [[ ! -f "$template" ]]; then
        echo "  ! template missing: $template" >&2
        return 1
    fi

    # Unload existing first (ignore errors).
    launchctl unload "$dest" 2>/dev/null || true

    sed -e "s|{{NETMONITOR_DIR}}|$NM_DIR|g" \
        -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
        "$template" > "$dest"

    launchctl load "$dest"
    echo "  installed: $label"
}

echo
echo "Installing services:"
install_one "$(nm_label_for monitor)"
install_one "$(nm_label_for cleanup)"
if (( WITH_DASHBOARD )); then
    install_one "$(nm_label_for dashboard)"
fi

echo
echo "Done. Loaded jobs:"
launchctl list | awk 'NR==1 || /com\.swarad\.netmonitor/'
