#!/usr/bin/env bash
# Start the Flask dashboard in the foreground. Ctrl+C to stop.
# For a persistent dashboard, run: ./nm install --with-dashboard

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if lsof -ti:8080 >/dev/null 2>&1; then
    echo "Port 8080 is already in use:"
    lsof -i:8080 | head
    echo
    echo "If that's the netmonitor dashboard from a previous run, kill it with:"
    echo "  lsof -ti:8080 | xargs kill"
    exit 1
fi

echo "Dashboard starting on http://localhost:8080  (Ctrl+C to stop)"
exec "$(nm_python_bin)" "$NM_DIR/app.py"
