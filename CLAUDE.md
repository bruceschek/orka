# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Orka is a read-only monitor for the other Claude Code projects on Bruce's Mac. It produces one scorecard (state, recent progress, blockers per project). Phases 1 (collector + scorecard) and 2 (hooks + event log) are built; phases 3–6 (Orka session, alerts, nudges, MBP move) are listed in `README.md`.

The collector uses no AI and no tokens: it only reads files, git and processes. Keep it that way.

## Commands

Everything is one script run with `uv` (PEP 723 inline deps, no project venv or test suite):

```sh
uv run orka.py show              # collect and print the scorecard
uv run orka.py collect           # collect, write ~/dev/.orka/scorecard.json + orka.db
uv run orka.py watch list|add <id> [aliases] [--machine mbp] [--paused]|remove <id>|pause <id>|resume <id>
uv run orka.py events [n]        # last n hook events
uv run orka.py install-hooks [--apply]   # Bruce runs this, not Claude (see below)
```

Claude may not edit `~/.claude/settings.json` (the auto-mode classifier blocks self-modifying permissions/hooks) and Bruce's auto mode blocks file access outside `~/dev`. That is why `state_dir` is `~/dev/.orka`, not `~/.orka`. Test with `ORKA_STATE_DIR` pointing at a scratch folder so real data stays clean, and for `install-hooks` set `HOME` to a fake home.

For testing against fake data, override paths with env vars: `ORKA_PROJECTS_ROOT`, `ORKA_CLAUDE_DIR`, `ORKA_STATE_DIR` (`samples/` is gitignored for this).

## Architecture

All logic lives in `orka.py`, in this order: config → helpers → sensors → state → collect/save/show → `watch` subcommands.

- **Config split:** `orka.toml` holds host settings (machine name, paths, thresholds). Moving Orka to another Mac should only require editing that file. `watchlist.toml` is the list of monitored projects.
- **Project lookup:** watchlist entries are matched by four-digit number prefix (`0071-*` under `projects_root`), not by full folder name, so renames don't break them. `machine` on an entry decides whether this Mac reads it; others get state `Remote`, and an unmatched folder gets `Missing`.
- **Sensors** gather facts per project: `git_info`, `newest_mtime` (file activity), `plan_info` (scans plan/TODO-style files for unchecked items and "needs Bruce"-type phrases), `session_info`, and `live_claude_cwds` (running `claude` processes via `lsof`).
- **Session logs:** `session_info` reads only the last ~400KB of the newest `.jsonl` in `~/.claude/projects/<encoded path>/`. The folder name is the project path with every non-alphanumeric character replaced by `-` (`encode_claude_dir`). It pulls the last user prompt, last Claude message, latest TodoWrite state, and whether the last assistant event is a `tool_use` with no result.
- **Live sessions:** `live_claude_cwds` finds `claude` processes with `ps`, then their working folders with `lsof -p`. `lsof -c claude` does not work, because the kernel names the process after the versioned binary (e.g. `2.1.330`).
- **Hooks (phase 2):** `orka_hook.py` runs with the system `/usr/bin/python3` (3.9, stdlib only, so no 3.10+ syntax), appends one JSON line per event to `<state_dir>/events.jsonl`, never prints and always exits 0. It must never store prompts or tool inputs. The collector replays the log (`hook_sessions`) into a per-session state (`working`, `needs_you`, `ready`, `ended`). `project_sessions` keeps sessions whose `cwd` is inside the project, only while a live `claude` process is in that folder (SessionEnd can be missed).
- **State machine:** `decide_state` goes in priority order: Paused → hook sessions if any (needs_you → Waiting on you, working → Active, else Idle "your turn") → phase 1 guess (live session + pending tool call + quiet >2 min) → Active (<`active_minutes`) → Idle → Stale (>`stale_days`).
- **Existing hooks:** Bruce's global settings already run `~/.config/iterm2/cc-status` on every event (iTerm status). `install-hooks` adds Orka's hook as a separate group and leaves those alone.
- **Output:** `scorecard.json` and `orka.db` go to `~/dev/.orka/` (`state_dir`), not into the repo. `.gitignore` excludes `*.db` and `scorecard.json`.
