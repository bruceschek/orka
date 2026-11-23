# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Orka is a read-only monitor for the other Claude Code projects on Bruce's Mac. It produces one scorecard (state, recent progress, blockers per project). Phase 1 (collector + scorecard) is built; phases 2–6 (hooks, Orka session, alerts, nudges, MBP move) are listed in `README.md`.

The collector uses no AI and no tokens: it only reads files, git and processes. Keep it that way.

## Commands

Everything is one script run with `uv` (PEP 723 inline deps, no project venv or test suite):

```sh
uv run orka.py show              # collect and print the scorecard
uv run orka.py collect           # collect, write ~/.orka/scorecard.json + orka.db
uv run orka.py watch list|add <id> [aliases] [--machine mbp] [--paused]|remove <id>|pause <id>|resume <id>
```

For testing against fake data, override paths with env vars: `ORKA_PROJECTS_ROOT`, `ORKA_CLAUDE_DIR`, `ORKA_STATE_DIR` (`samples/` is gitignored for this).

## Architecture

All logic lives in `orka.py`, in this order: config → helpers → sensors → state → collect/save/show → `watch` subcommands.

- **Config split:** `orka.toml` holds host settings (machine name, paths, thresholds). Moving Orka to another Mac should only require editing that file. `watchlist.toml` is the list of monitored projects.
- **Project lookup:** watchlist entries are matched by four-digit number prefix (`0071-*` under `projects_root`), not by full folder name, so renames don't break them. `machine` on an entry decides whether this Mac reads it; others get state `Remote`, and an unmatched folder gets `Missing`.
- **Sensors** gather facts per project: `git_info`, `newest_mtime` (file activity), `plan_info` (scans plan/TODO-style files for unchecked items and "needs Bruce"-type phrases), `session_info`, and `live_claude_cwds` (running `claude` processes via `lsof`).
- **Session logs:** `session_info` reads only the last ~400KB of the newest `.jsonl` in `~/.claude/projects/<encoded path>/`. The folder name is the project path with every non-alphanumeric character replaced by `-` (`encode_claude_dir`). It pulls the last user prompt, last Claude message, latest TodoWrite state, and whether the last assistant event is a `tool_use` with no result.
- **State machine:** `decide_state` combines the sensors in priority order: Paused → Waiting on you (live session + pending tool call + quiet >2 min; a heuristic until phase 2 hooks) → Active (<`active_minutes`) → Idle → Stale (>`stale_days`).
- **Output:** `scorecard.json` and `orka.db` go to `~/.orka/` (`state_dir`), not into the repo. `.gitignore` excludes `*.db` and `scorecard.json`.
