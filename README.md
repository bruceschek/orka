# Orka

Orka watches the Claude Code projects on the watchlist and gives one scorecard:
state, recent progress and blockers for each project.

## Files

| File | Purpose |
|---|---|
| `orka.toml` | Settings shared by both Macs, including each Mac's Tailscale address (`[machines]`). |
| `orka.local.toml` | Optional, not in git: this Mac's overrides. On the MBP: `machine = "mbp"`. |
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
uv run orka.py new tana graph sync [--machine mbp] [--github]   # preview a new project; add --apply
uv run orka.py clone 0104        # preview copying a project from GitHub to this Mac; add --apply
uv run orka.py alert-test        # preview a Pushover test message; add --apply to send it
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

## Both Macs (phase 4)

Each Mac runs its own Orka. Put each Mac's `tailscale serve` address under `[machines]` in
`orka.toml`. Every Mac then fetches the others' data from `/api/local` and shows their
projects and jobs too, tagged with the Mac's name, so either Mac's address on the phone
shows both Macs.

Either Mac may be off. Its data then stays on the dashboard, dimmed and marked
"offline · data as of …", and is never listed under "Needs you". The last copy is kept in
`~/dev/.orka/peer-<name>.json`, so it survives a restart. When that Mac answers again
(retried every 10 seconds), it is shown as live again by itself.

`/api/scorecard` is the combined view; `/api/local` is this Mac only.

## Phone alerts (phase 4)

While `serve` runs, Orka sends a Pushover message to your phone when a project has been
"Waiting on you", or a job has failed, for 2 minutes (`[alerts]` in `orka.toml`). Each event
alerts once; when it clears, a later one alerts again. Only one Mac sends: the MBA, or the
MBP while the MBA is off. An offline Mac's old data never alerts. Tapping the alert opens
that Mac's dashboard. The footer of the dashboard shows the alert status.

Setup on each Mac: put your Pushover User Key and API Token in `~/dev/.orka/pushover.toml`
(private, not in git), as `user_key = "…"` and `api_token = "…"`, then
`uv run orka.py alert-test --apply`. The phone needs the Pushover app, logged in to the same
account. If nothing arrives, check that the account lists the phone under "Your Devices".

## Controls (phase 5)

Each project card on the dashboard has a **Pause** or **Resume** button. A paused project
shows "Paused" and sends no alerts. The Mac that owns the project does it (the other Mac
passes the request on), updates `watchlist.toml`, commits and pushes it. If the owning Mac
is off, the button is greyed out.

Only your Tailscale login may use controls. It is listed in `~/dev/.orka/controls.toml` on
each Mac (private, because this repo is public): `allowed_logins = ["you@example.com"]`.
Requests from other websites are refused. Every attempt, allowed or not, is logged to
`~/dev/.orka/actions.jsonl`.

## New projects and moving them between Macs

Project numbers are one sequence across both Macs, and every project folder has the same
path on each Mac (`~/dev/projects/NNNN-name`). `watchlist.toml` doubles as the number
registry, so either Mac can start a project even when the other is off.

`orka.py new <name> --apply` pulls Orka's repo, takes the next number (the highest of
`last_number`, the watchlist and this Mac's folders, plus one), creates the folder with
`git init`, adds the project to the watchlist and pushes that straight back. With
`--github` it also creates a private GitHub repo and records its URL as `repo`. With
`--machine mbp` it only registers the number for the other Mac. The last line printed is
the new folder, so a shell function can `cd` into it.

`orka.py clone <id> --apply` copies a project from its `repo` to the same folder name on
this Mac and adds this Mac to the project's `machine` list. Code moves between Macs only
through GitHub; never sync `~/dev` with Dropbox or iCloud.

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
3. Read-only dashboard (`serve`) on the MBA, phone access over Tailscale, `jobs.toml`, `/scorecard` skill (done)
4. Orka on the M1; each Mac's dashboard also shows the other Mac. Phone alerts for "waiting on you" and failed jobs. `new`/`clone` with one project numbering for both Macs. (done, 0.5.0)
5. Controls: pause/resume projects (done, 0.6.0); start/stop/restart jobs; open or start a Claude session in iTerm; nudge a session. Every action logged.
6. Answer Claude permission requests from the dashboard (with a login); daily digest.
