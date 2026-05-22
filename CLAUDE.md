# CLAUDE.md — notes for AI sessions working on this repo

This is a personal tool that probes the user's home internet at three layers
and renders attributed outages. The user is **Swarad Mokal** (Technical
Program Manager at Axelerant); see his global CLAUDE.md for personal
preferences (concise, no fluff, ask before doing critical/destructive things).

## Read these first — don't duplicate them here

- [`README.md`](README.md) — what the tool does, file layout, schema, CLI,
  architecture diagram, troubleshooting. If you need to know *what* something
  is, read this.
- [`DECISIONS.md`](DECISIONS.md) — *why* every non-obvious choice was made
  (probe interval, SQLite, sessions model, vendored Plotly, no auto-VACUUM,
  CGNAT handling, etc.). **Always read this before changing core behavior.**

Treat this file as AI-only operational notes. If something can be learned by
reading `README.md`, `DECISIONS.md`, or the code itself, it does *not* belong
here.

## Strict rules

- **Never add Claude (or any AI) as a co-author or collaborator on commits.**
  No `Co-Authored-By: Claude …` trailer, no "🤖 Generated with…" footer,
  no mention of AI authorship anywhere in commit messages or PR bodies.
- **Prefer multiple small commits over one combined commit.** Split by
  concern — different files, different topics, different intents each get
  their own commit. The only reason to combine is when the changes are in
  the *same file* and inseparable.
- **Don't drop legacy tables on schema changes.** `init_db` in `monitor.py`
  already renames old `probes` without `session_id` to `probes_legacy_v1`.
  Mirror that pattern for any future migration. If you change schema, also
  update `cleanup.py`, `app.py` API queries, and `bin/storage.sh`.

## How to test changes

```bash
./nm reload                    # restart monitor daemon to pick up code changes
./nm status                    # confirm it came back up
./nm logs monitor              # tail and watch
./nm dashboard                 # open the UI to verify visually
```

For changes that touch SQL or schema, also run cleanup once:

```bash
./nm cleanup
./nm storage                   # confirm rows still consistent
```

## AI-specific gotchas

1. **launchd cache.** Editing a plist does nothing on its own —
   `launchctl unload` + `launchctl load` (or `./nm reload`) is required.
2. **pyenv shims in launchd.** launchd has no shell PATH, so the plist must
   reference an *absolute* python path. `bin/install.sh` resolves it via
   `python3 -c 'import sys; print(sys.executable)'` at install time. After
   reorganizing files, re-run `./nm install` to re-render the plist.
3. **The user's running daemon.** The launchd jobs are already loaded on the
   user's machine. Prefer additive changes (new files, renames in repo) over
   disrupting what `launchctl` has already loaded — check `./nm status`
   before touching plists.
4. **Geolocation API.** `ip-api.com` HTTP, no key, ≤1 call per session.
   Don't put it in the per-probe path.

## Conventions

- Bash scripts: `set -euo pipefail`, source `bin/_common.sh`.
- Python: stdlib-only in `monitor.py` and `cleanup.py`. `app.py` uses Flask.
  Keep it that way unless there's a strong reason — adding deps to the
  monitor risks the launchd job failing to start on a system where they're
  missing.
- Comments: only when the *why* is non-obvious (see `DECISIONS.md` style).
- Don't add features the user didn't ask for. This repo grew from a 50-line
  pinger; resist scope creep.

## When the user reports an issue

Ask first:

- What does `./nm status` say?
- What's in the last 20 lines of `monitor.log`?
- Did the network change recently (would create a new session row)?

Don't immediately `rm` the DB or unload launchd jobs — those are destructive
and contain history the user may need for an ISP call.
