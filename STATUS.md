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

Bruce's answers (2026-11-24):
- The M1 has no launchd agents yet; they will be added later.
- Both Macs are usually on, but either or both may sometimes be shut down. Everything must
  handle that (see the "Machines can be off" rule in `CLAUDE.md`).

- [x] Each Mac's dashboard also shows the other Mac (built 2026-11-24, collector 0.4.0; tested
      with two local `serve`s: both online, MBP off → offline after ~80s, MBA restart keeps the
      MBP's last copy, MBP back on → live again in ~9s).
- [x] `orka.py new` / `clone` (2026-11-24): one project-number sequence for both Macs, registry
      in `watchlist.toml` (`last_number = "0103"` set by hand from the MBA's folders).
- [x] Bruce: point his `newproj` shell function (in `~/.zshrc`) at `orka.py new --apply`
      (2026-11-24: active via command-line-tools `projects.zsh`; `nextnum` now returns 0104 from Orka).
- [x] On the MBP first: `whoami; git --version; uv --version; claude --version; ls -d /Applications/Tailscale.app`,
      then follow `M1-SETUP.md` in the command-line-tools repo (prompt, status line, `newproj`). (2026-11-24)
- [x] Install Orka on the MBP: clone to `~/dev/projects/0102-orka`, create `orka.local.toml`
      with `machine = "mbp"`, hooks, launchd agent, `tailscale serve --bg 8765`; then put the
      MBP's address under `[machines]` in `orka.toml`, commit, and pull on both Macs. (2026-11-24:
      MBP serving at `https://bruces-m1-mbp-2111.taila48a19.ts.net`; dashboard shows MBA peer online.)
- [ ] After that, restart `serve` on the MBA (launchd agent) so it picks up the new code.
- [ ] Phone alerts for "waiting on you" and failed jobs.

## Working notes for Claude

- Bruce wants setup done step by step: one command at a time, checked before the next.
- Claude cannot run setup that touches `~/.claude/settings.json`, `~/Library/LaunchAgents`,
  or paths outside `~/dev` (auto mode blocks it). Give Bruce the command with a dry run first.
- Commands that may prompt or wait (tailscale, logins) belong in iTerm, not `!` in Claude Code.
