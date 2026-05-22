#!/usr/bin/env bash
# Tail one of the logs. Usage: logs [monitor|cleanup|dashboard]  (default: monitor)

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

target="${1:-monitor}"
case "$target" in
    monitor)   files=("$NM_DIR/monitor.log"   "$NM_DIR/monitor.err.log") ;;
    cleanup)   files=("$NM_DIR/cleanup.log"   "$NM_DIR/cleanup.err.log") ;;
    dashboard) files=("$NM_DIR/dashboard.log" "$NM_DIR/dashboard.err.log") ;;
    all)       files=("$NM_DIR"/*.log) ;;
    *) echo "unknown: $target"; exit 2 ;;
esac

existing=()
for f in "${files[@]}"; do
    [[ -f "$f" ]] && existing+=("$f")
done

if [[ ${#existing[@]} -eq 0 ]]; then
    echo "(no log files for $target yet)"
    exit 0
fi

echo "tailing: ${existing[*]}"
echo "----"
exec tail -F "${existing[@]}"
