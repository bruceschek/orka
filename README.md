# Orka

Orka watches the Claude Code projects on the watchlist and gives one scorecard:
state, recent progress and blockers for each project.

## Files

| File | Purpose |
|---|---|
| `orka.toml` | Host settings. Change only this file to move Orka to another Mac. |
| `watchlist.toml` | The projects that Orka monitors. Orka finds each folder by its number. |
| `orka.py` | Collector and watchlist tool. Uses no AI and no tokens. |
| `orka_hook.py` | Hook that Claude Code runs on session events. Adds one line per event to `~/.orka/events.jsonl`. |
| `~/.orka/` | Runtime data on each Mac: `scorecard.json`, `orka.db`, `events.jsonl`. |

## Commands

```sh
uv run orka.py show              # collect now and print the scorecard
uv run orka.py collect           # collect now, write ~/.orka/scorecard.json
uv run orka.py watch list
uv run orka.py watch add 0099 alias1 alias2
uv run orka.py watch remove 0099
uv run orka.py watch pause 0099  # still monitored, no alerts
uv run orka.py events            # last 30 hook events (add a number for more)
uv run orka.py install-hooks     # preview the hook setup; add --apply to install
```

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

1. Collector and scorecard (done)
2. Global hooks and event log (this version)
3. Orka session and `/scorecard` skill
4. Remote Control, push alerts and daily digest
5. Nudge with approval
6. Move to the MBP M1
