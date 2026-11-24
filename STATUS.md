# Orka status and restart notes

Last updated 2026-11-23. Read this first when resuming after the MBA rebuild.

## Where things stand

- Phases 1–3 are built, committed and pushed (`github.com/bruceschek/orka`, branch `main`).
- The MBA was rebuilt and Orka restored on it (2026-11-23): `orka.toml` checked, hooks firing
  (`uv run orka.py events`), dashboard launchd agent answering on `http://127.0.0.1:8765`.
- Phone access works from the Mac side: `tailscale serve --bg 8765` serves
  `https://bruce-mbair-26-04.taila48a19.ts.net/` (Claude verified `/api/health` over HTTPS).
  `--bg` keeps the config across restarts; turn it off with `tailscale serve --https=443 off`.
- The old `tailscale serve` hang was most likely because the Mac was logged out of Tailscale
  and/or HTTPS certificates were off. After the rebuild the Tailscale app was installed but
  not running; `open -a Tailscale`, log in, then enable MagicDNS + HTTPS in the admin DNS page.

## Phase 3 phone access: done

- [x] iPhone (Tailscale connected) shows the live dashboard at
      `https://bruce-mbair-26-04.taila48a19.ts.net/` (confirmed by Bruce 2026-11-24).
      In Safari's top-bar layout, Share is under the ≡ button left of the address.

## Next: phase 4

- [ ] Bruce lists the M1's launchd agents and long-running scripts (and whether the M1 is always on;
      unanswered so far). Then install Orka on the M1 and make each dashboard show both Macs.

## Working notes for Claude

- Bruce wants setup done step by step: one command at a time, checked before the next.
- Claude cannot run setup that touches `~/.claude/settings.json`, `~/Library/LaunchAgents`,
  or paths outside `~/dev` (auto mode blocks it). Give Bruce the command with a dry run first.
- Commands that may prompt or wait (tailscale, logins) belong in iTerm, not `!` in Claude Code.
