# Orka

Orka watches the Claude Code projects on the watchlist and gives one scorecard:
state, recent progress and blockers for each project.

## Files

| File | Purpose |
|---|---|
| `orka.toml` | Host settings. Change only this file to move Orka to another Mac. |
| `watchlist.toml` | The projects that Orka monitors. Orka finds each folder by its number. |
| `jobs.toml` | Non-Claude jobs Orka reports on: launchd agents and long-running scripts, per Mac. |
| `orka.py` | Collector, dashboard server and watchlist tool. Uses no AI and no tokens. |
| `dashboard.html` | The dashboard page that `orka.py serve` shows (phone-first, no outside libraries). |
| `.claude/skills/scorecard/` | `/scorecard` skill: Claude runs the collector and reports in plain language. |
| `orka_hook.py` | Hook that Claude Code runs on session events. Adds one line per event to `~/dev/.orka/events.jsonl`. |
| `~/dev/.orka/` | Runtime data on each Mac: `scorecard.json`, `orka.db`, `events.jsonl`. Inside `~/dev` so Claude can read it. |

## Commands

```sh
uv run orka.py show              # collect now and print the scorecard
uv run orka.py collect           # collect now, write ~/dev/.orka/scorecard.json
uv run orka.py watch list
uv run orka.py watch add 0099 alias1 alias2
uv run orka.py watch remove 0099
uv run orka.py watch pause 0099  # still monitored, no alerts
uv run orka.py events            # last 30 hook events (add a number for more)
uv run orka.py install-hooks     # preview the hook setup; add --apply to install
uv run orka.py serve             # dashboard on http://127.0.0.1:8765
uv run orka.py install-agent     # preview keeping `serve` running at login; add --apply to install
```

## Dashboard and phone access (phase 3)

`orka.py serve` collects every 30 seconds and serves the dashboard and
`/api/scorecard` on `127.0.0.1` only. It is read-only. To keep it running, run
`uv run orka.py install-agent --apply` once (a launchd agent, log in
`~/dev/.orka/serve.log`).

For your phone, publish it to your Tailscale network (never the public internet):

```sh
tailscale serve --bg 8765
```

Then open `https://<this-mac>.<your-tailnet>.ts.net` on the phone and use
Share → Add to Home Screen. Only devices signed in to your tailnet can reach it.

## Hooks (phase 2)

`uv run orka.py install-hooks --apply` adds `orka_hook.py` to the global
`~/.claude/settings.json` for these events: SessionStart, UserPromptSubmit,
PostToolUse, PermissionRequest, Notification, Stop, StopFailure, SessionEnd.
It keeps every hook already there, backs up the old file to
`settings.json.orka-backup`, and does nothing if Orka's hook is already installed.
Run it yourself: Claude is not allowed to change its own settings. Sessions that
were open before the install report only after a restart.

The hook stores the event name, session ID, folder, time, and for some events
the tool name or notification text. It never stores your prompts or tool inputs.
It always exits 0 and prints nothing, so it cannot block a session.
Projects whose sessions have no hook events fall back to the phase 1 guess.

## Project states

| State | Meaning |
|---|---|
| Waiting on you | A session asks for your approval or an answer. Exact with hooks; without them, a guess from the session log. |
| Active | Claude is working (from hooks), or activity in the last 15 minutes. |
| Idle | No recent activity, or Claude finished its reply and it's your turn. A session can be open or closed. |
| Stale | No activity for 14 days. |
| Paused | You put the project on hold in the watchlist. |

## Build phases

Goal: one dashboard, on the phone, that reports on Claude sessions and other
jobs on both Macs, and lets you control them.

1. Collector and scorecard (done)
2. Global hooks and event log (done)
3. Read-only dashboard (`serve`) on the MBA, phone access over Tailscale, `jobs.toml`, `/scorecard` skill (this version)
4. Orka on the M1; each Mac's dashboard also shows the other Mac. Phone alerts for "waiting on you" and failed jobs.
5. Controls: pause/resume projects; start/stop/restart jobs; open or start a Claude session in iTerm; nudge a session. Every action logged.
6. Answer Claude permission requests from the dashboard (with a login); daily digest.
