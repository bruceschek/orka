# Orka status and restart notes

Last updated 2026-11-23. Read this first when resuming after the MBA rebuild.

## Where things stand

- Phases 1–3 are built, committed and pushed (`github.com/bruceschek/orka`, branch `main`).
- Phase 2 hooks proved themselves on real sessions: `PermissionRequest` events arrive
  (including for AskUserQuestion), as do Stop, UserPromptSubmit, PostToolUse, Notification.
- Phase 3 dashboard ran as a launchd agent and served correctly on `http://127.0.0.1:8765`.
- **Stopped at:** phone access. `tailscale serve --bg 8765` hung with no output, both inside
  Claude Code and in iTerm. Debugging Tailscale is why the Mac is being rebuilt.

## After the rebuild: restore the machine setup

None of this lives in the repo; it must be redone on the new system.

- [ ] Clone the repo to `~/dev/projects/0102-orka` (Orka finds projects by number under `~/dev/projects`).
- [ ] Check `orka.toml`: `machine = "mba"`, `state_dir = "~/dev/.orka"`.
- [ ] Hooks: `uv run orka.py install-hooks`, then `--apply`. It adds Orka's hook to the global
      `~/.claude/settings.json` next to existing hooks (Bruce's iTerm `cc-status` hook, if restored).
- [ ] Dashboard: `uv run orka.py install-agent`, then `--apply` (launchd agent `com.<user>.orka`).
      Claude checks it with `curl http://127.0.0.1:8765/api/health`.
- [ ] Optional: recreate `.claude/settings.json` in this repo (not committed; local permission
      rules): allow `Bash(git status:*)`, `Bash(git log:*)`, `Bash(git diff:*)`, `Bash(git show:*)`.
- [ ] Optional: `uv run orka.py events` after a few minutes of Claude use, to confirm hooks fire.

`~/dev/.orka/` (events, scorecard, history db) is disposable; Orka rebuilds it.

## Then continue phase 3: phone access over Tailscale

- [ ] In iTerm (not inside Claude Code: it hides prompts): `tailscale status` must list devices.
      If not, open the Tailscale menu bar app and connect.
- [ ] Tailscale admin (login.tailscale.com → DNS): MagicDNS on, HTTPS Certificates enabled.
- [ ] `tailscale serve --bg 8765` → note the `https://<mac>.<tailnet>.ts.net` address.
- [ ] Open that address on the phone, Share → Add to Home Screen.

## Next: phase 4

- [ ] Bruce lists the M1's launchd agents and long-running scripts (and whether the M1 is always on;
      unanswered so far). Then install Orka on the M1 and make each dashboard show both Macs.

## Working notes for Claude

- Bruce wants setup done step by step: one command at a time, checked before the next.
- Claude cannot run setup that touches `~/.claude/settings.json`, `~/Library/LaunchAgents`,
  or paths outside `~/dev` (auto mode blocks it). Give Bruce the command with a dry run first.
- Commands that may prompt or wait (tailscale, logins) belong in iTerm, not `!` in Claude Code.
