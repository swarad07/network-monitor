#!/usr/bin/env bash
# Fetch vendored runtime assets that are .gitignored (Plotly bundle).
# Run once after cloning; idempotent on re-run.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PLOTLY_DEST="$NM_DIR/static/plotly.min.js"
PLOTLY_URLS=(
    "https://unpkg.com/plotly.js-dist-min@2.35.2/plotly.min.js"
    "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"
)

mkdir -p "$NM_DIR/static"

if [[ -s "$PLOTLY_DEST" ]]; then
    size=$(stat -f%z "$PLOTLY_DEST")
    if (( size > 1000000 )); then
        echo "Plotly already present ($size bytes) at $PLOTLY_DEST"
        exit 0
    fi
    echo "Plotly file exists but seems too small ($size bytes), re-downloading..."
fi

for url in "${PLOTLY_URLS[@]}"; do
    echo "Trying $url ..."
    if curl -fsSL -o "$PLOTLY_DEST.tmp" -m 60 "$url"; then
        size=$(stat -f%z "$PLOTLY_DEST.tmp")
        if (( size > 1000000 )); then
            mv "$PLOTLY_DEST.tmp" "$PLOTLY_DEST"
            echo "Downloaded $size bytes -> $PLOTLY_DEST"
            exit 0
        fi
        echo "  too small ($size bytes), likely an ISP block page; trying next..."
        rm -f "$PLOTLY_DEST.tmp"
    fi
done

echo "ERROR: could not download Plotly from any source." >&2
echo "If your ISP filters cdn.plot.ly (as has happened before), try a hotspot or VPN." >&2
exit 1
