# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Orka is a read-only monitor for the other Claude Code projects on Bruce's Mac. It produces one scorecard (state, recent progress, blockers per project). Phases 1 (collector), 2 (hooks + event log) and 3 (read-only dashboard, jobs, `/scorecard`) are built. The goal is a phone dashboard covering Claude sessions and other jobs on both Macs, with controls. The Macs: MBA = 15" MacBook Air M5, 32 GB (April 2026, `machine = "mba"`, Tailscale name `bruce-mbair-26-04`); MBP = 16" MacBook Pro M1 Max, 32 GB (Nov 2021), which Bruce also calls "the M1" (`machine = "mbp"`). More detail is in Bruce's Tana ("Bruce - M5 MBAir 26-04", "Bruce's M1 MBP 21-11"). Phases 4–6 are in `README.md`.

The collector uses no AI and no tokens: it only reads files, git and processes. Keep it that way.

**Orca compatibility:** Bruce is building Orka on its own for now. Later he may run the open-source Orca (https://orca.dev) alongside it, so the two coexist and help each other; Orca might take over some parts of Orka while the rest stays. When there is a choice of design, pick the one that would let Orca replace or plug into a part of Orka with little work: clear boundaries between parts (collection, event log, state rules, dashboard API, controls) and open data formats (JSON, HTTP). Mention it when a decision affects this. Don't build speculative Orca integration.

**Machines can be off:** the MBA and the M1 are usually on, but either or both can be shut down at any time. Nothing may depend on the other Mac being up: each Mac runs its own Orka, shows the other Mac's last known data marked as offline/stale when it can't be reached, and recovers on its own when it comes back.

**Resuming work? Read `STATUS.md` first:** current stopping point, machine setup to redo, and next steps.

## Commands

Everything is one script run with `uv` (PEP 723 inline deps, no project venv or test suite):

```sh
uv run orka.py show              # collect and print the scorecard
uv run orka.py collect           # collect, write ~/dev/.orka/scorecard.json + orka.db
uv run orka.py watch list|add <id> [aliases] [--machine mbp] [--paused]|remove <id>|pause <id>|resume <id>
uv run orka.py events [n]        # last n hook events
uv run orka.py install-hooks [--apply]   # Bruce runs this, not Claude (see below)
uv run orka.py serve             # dashboard; test on another port with ORKA_PORT=8799
uv run orka.py install-agent [--apply]   # launchd agent for serve; Bruce runs this too
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
- **Jobs:** `jobs.toml` lists launchd agents (read via `launchctl print gui/<uid>/<label>`, top-level fields only) and scripts (regex on `ps` args), each with a `machine`. Jobs for the other Mac show as `remote`.
- **Dashboard:** `serve` runs a background collect every `refresh_seconds`, keeps the last good card in memory, and serves `dashboard.html`, `/api/scorecard` (all Macs), `/api/local` (this Mac only) and `/api/health` from `127.0.0.1` only. Phone access is `tailscale serve`, which adds the `Tailscale-User-Login` header (shown as `viewer`). It writes an `orka.db` row only every 10 minutes. The page builds the DOM with `textContent` only; keep it that way, because the data contains project text. Controls (phase 5) must be logged and should check the Tailscale identity.
- **Other Macs (phase 4):** `[machines]` in `orka.toml` maps each Mac to its Tailscale URL; `orka.local.toml` (gitignored) overrides per Mac, e.g. `machine = "mbp"`. `serve` runs one `peer_loop` thread per other Mac that fetches its `/api/local` (never `/api/scorecard`, so data doesn't bounce back), checks the answer names the expected machine, and caches it in `<state_dir>/peer-<name>.json`. `merge_peers` replaces each `Remote` project/job with the peer's record, tagged `from_machine`, `as_of`, `peer_offline` (no success for `max(90s, 3×refresh)`). The page dims offline records and keeps them out of "Needs you". Test with two `serve`s on ports 8798/8799 using `ORKA_MACHINE`, `ORKA_PEERS`, `ORKA_WATCHLIST`, `ORKA_JOBS`, `ORKA_STATE_DIR`.
- **Event log size:** `rotate_events` renames `events.jsonl` to `events.1.jsonl` beyond 5 MB; `read_events` reads both.
- **Output:** `scorecard.json` and `orka.db` go to `~/dev/.orka/` (`state_dir`), not into the repo. `.gitignore` excludes `*.db` and `scorecard.json`.
