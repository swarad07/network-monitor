#!/usr/bin/env python3
"""Retention enforcement: keep RETENTION_DAYS of probe data, drop the rest.

Designed to run daily via launchd. Safe to interrupt — uses ordinary DELETEs
in autocommit mode, no long-running transaction.
"""
import sqlite3
import sys
import time
from pathlib import Path

RETENTION_DAYS = 56  # 8 weeks
DB_PATH = Path(__file__).parent / "netmonitor.db"


def main() -> int:
    if not DB_PATH.exists():
        print(f"[cleanup] DB not found at {DB_PATH}", file=sys.stderr)
        return 1

    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    conn = sqlite3.connect(DB_PATH, timeout=30.0)

    expired = conn.execute(
        "SELECT COUNT(*) FROM probes WHERE ts < ?", (cutoff,)
    ).fetchone()[0]

    if expired == 0:
        print(f"[cleanup] nothing to delete; cutoff={cutoff} retention={RETENTION_DAYS}d")
    else:
        conn.execute("DELETE FROM probes WHERE ts < ?", (cutoff,))
        conn.commit()
        print(f"[cleanup] deleted {expired} probe rows older than {RETENTION_DAYS}d (cutoff ts={cutoff})")

    # Drop sessions whose end is past retention AND have no probes referencing them.
    cur = conn.execute(
        """
        DELETE FROM sessions
        WHERE end_ts IS NOT NULL AND end_ts < ?
          AND id NOT IN (SELECT DISTINCT session_id FROM probes)
        """,
        (cutoff,),
    )
    orphans = cur.rowcount
    if orphans:
        conn.commit()
        print(f"[cleanup] deleted {orphans} orphan session rows")

    # Truncate the WAL so disk doesn't keep growing between vacuums.
    # Best-effort: TRUNCATE needs no other connection holding a transaction.
    # If the daemon is mid-write we just skip; daemon's auto-checkpoint catches up.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as e:
        print(f"[cleanup] wal_checkpoint skipped: {e}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
