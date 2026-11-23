#!/usr/bin/env python3
"""Orka hook. Claude Code runs this on session events; it appends one line to
the Orka event log (<state_dir>/events.jsonl).

Stdlib only and Python 3.9+, because it runs with the system python3 on every
event in every session. It never prints, never blocks and always exits 0, so it
cannot get in the way of a session. It uses no AI and no tokens.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Fields copied from the hook input. Prompts and tool inputs are left out on purpose.
KEEP = ("session_id", "cwd", "transcript_path", "notification_type", "tool_name",
        "source", "reason")


def state_dir() -> Path:
    env = os.environ.get("ORKA_STATE_DIR")
    if env:
        return Path(os.path.expanduser(env))
    try:  # tomllib is not in Python 3.9, so read the one line we need
        m = re.search(r'^\s*state_dir\s*=\s*"([^"]+)"', (HERE / "orka.toml").read_text(), re.M)
        if m:
            return Path(os.path.expanduser(m.group(1)))
    except OSError:
        pass
    return Path.home() / "dev" / ".orka"


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not data.get("session_id"):
        return  # not a Claude Code hook call; log nothing
    ev = {"ts": round(time.time(), 3),
          "event": data.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "unknown")}
    for k in KEEP:
        if data.get(k) is not None:
            ev[k] = data[k]
    if isinstance(data.get("message"), str):
        ev["message"] = data["message"][:200]
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    # One short write in append mode: safe when several sessions log at once.
    with open(d / "events.jsonl", "a") as fh:
        fh.write(json.dumps(ev) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
