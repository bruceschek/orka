---
name: scorecard
description: Report on Bruce's watched Claude Code projects and jobs from Orka's scorecard - what is waiting on him, what is moving, what is neglected. Use when Bruce runs /scorecard or asks what needs him, how a project is doing, or about a project by number or alias.
---

# Scorecard

1. Run `uv run orka.py collect` in the Orka folder (free: no AI, no tokens).
2. Read `~/dev/.orka/scorecard.json`. For recent session activity, run `uv run orka.py events 40`.
3. Answer in plain, short language, in this order, leaving out empty sections:
   - **Waiting on you**: projects in state "Waiting on you" (say what is waiting, from `note`),
     failed or not-loaded jobs, and the most important `plans.needs_you` items.
   - **Moving**: Active projects and what they are doing (`sessions`, `session.todos.in_progress`, last commit).
   - **Slipping**: Stale projects, many dirty files, unpushed commits (`git.ahead`), jobs that have not run when expected.
   - One line for everything else ("6 idle, nothing pending").
4. Name projects by number and short name (`0083 SVBIG`), not full folder names.

If Bruce asks about one project, match the number or an alias in `watchlist.toml`, then also
read that project's plan files (listed in `plans.files`, relative to `path`) for detail.

Do not change anything in watched projects, jobs, or settings. This skill only reports.
If `self_check` shows a problem (hooks silent, session logs unreadable), say so first.
