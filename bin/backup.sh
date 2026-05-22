#!/usr/bin/env bash
# Snapshot the DB to ./backups/ using sqlite's safe online backup.
# Safe to run while the daemon is writing.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if [[ ! -f "$NM_DB" ]]; then echo "DB not found: $NM_DB"; exit 1; fi

mkdir -p "$NM_DIR/backups"
stamp="$(date +%Y%m%d-%H%M%S)"
dest="$NM_DIR/backups/netmonitor-$stamp.db"

echo "Backing up $NM_DB -> $dest"
sqlite3 "$NM_DB" ".backup '$dest'"
size=$(du -h "$dest" | cut -f1)
echo "Done. $dest ($size)"

# Keep most recent 14 backups, prune the rest. (bash 3.2-compatible — no mapfile.)
keep=14
old=()
while IFS= read -r f; do
    old+=("$f")
done < <(ls -1t "$NM_DIR"/backups/netmonitor-*.db 2>/dev/null | tail -n +$((keep + 1)))
if [[ ${#old[@]} -gt 0 ]]; then
    echo "Pruning ${#old[@]} old backup(s):"
    for f in "${old[@]}"; do echo "  rm $f"; rm -f "$f"; done
fi
