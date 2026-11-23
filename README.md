# Orka

Orka watches the Claude Code projects on the watchlist and gives one scorecard:
state, recent progress and blockers for each project.

## Files

| File | Purpose |
|---|---|
| `orka.toml` | Host settings. Change only this file to move Orka to another Mac. |
| `watchlist.toml` | The projects that Orka monitors. Orka finds each folder by its number. |
| `orka.py` | Collector and watchlist tool. Uses no AI and no tokens. |
| `~/.orka/` | Runtime data on each Mac: `scorecard.json`, `orka.db`, later events and logs. |

## Commands

```sh
uv run orka.py show              # collect now and print the scorecard
uv run orka.py collect           # collect now, write ~/.orka/scorecard.json
uv run orka.py watch list
uv run orka.py watch add 0099 alias1 alias2
uv run orka.py watch remove 0099
uv run orka.py watch pause 0099  # still monitored, no alerts
```

## Project states

| State | Meaning |
|---|---|
| Waiting on you | A session is open and its last step is a tool call with no result (usually an approval). Phase 2 hooks make this exact. |
| Active | Activity in the last 15 minutes. |
| Idle | No recent activity. A session can be open or closed. |
| Stale | No activity for 14 days. |
| Paused | You put the project on hold in the watchlist. |

## Build phases

1. Collector and scorecard (this version)
2. Global hooks and event log
3. Orka session and `/scorecard` skill
4. Remote Control, push alerts and daily digest
5. Nudge with approval
6. Move to the MBP M1
