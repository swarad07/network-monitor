#!/usr/bin/env bash
# Run retention cleanup immediately. Same logic launchd runs at 03:30 daily.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

exec "$(nm_python_bin)" "$NM_DIR/cleanup.py" "$@"
