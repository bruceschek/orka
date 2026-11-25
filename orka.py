#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["tomli>=2; python_version < '3.11'"]
# ///
"""Orka collector and watchlist tool.

Usage (from the Orka folder):
  uv run orka.py collect            # read all watched projects, write scorecard.json + orka.db
  uv run orka.py show               # collect, then print a text scorecard
  uv run orka.py watch list
  uv run orka.py watch add 0099 [alias ...] [--machine mbp] [--paused]
  uv run orka.py watch remove 0099
  uv run orka.py watch pause 0099 | resume 0099
  uv run orka.py events [n]         # last n hook events (default 30)
  uv run orka.py new <name> [--machine mbp] [--github] [--apply]  # next number, folder, watchlist
  uv run orka.py clone <id> [--apply]     # copy a project from GitHub to the same folder here
  uv run orka.py alert-test [--apply]     # send one test message through Pushover
  uv run orka.py serve              # dashboard on http://127.0.0.1:8765 (phone: tailscale serve)
  uv run orka.py install-agent [--apply]  # keep `serve` running at login (launchd)
  uv run orka.py install-hooks [--apply]  # add Orka's hook to ~/.claude/settings.json

The collector uses no AI and no tokens. It only reads files, git and processes.
Environment overrides (for testing): ORKA_PROJECTS_ROOT, ORKA_CLAUDE_DIR, ORKA_STATE_DIR,
ORKA_MACHINE, ORKA_PORT, ORKA_WATCHLIST, ORKA_JOBS, ORKA_PEERS ("mbp=http://127.0.0.1:8799,...").
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

VERSION = "0.7.0"
HERE = Path(__file__).resolve().parent
CONFIG = HERE / "orka.toml"
CONFIG_LOCAL = HERE / "orka.local.toml"  # per-Mac overrides, not in git (e.g. machine = "mbp")
WATCHLIST = Path(os.environ.get("ORKA_WATCHLIST") or HERE / "watchlist.toml")
JOBS = Path(os.environ.get("ORKA_JOBS") or HERE / "jobs.toml")
DASHBOARD = HERE / "dashboard.html"

PLAN_NAMES = re.compile(
    r"^(plan|todo|backlog|handoff|status|release-status|roadmap|next|tasks|progress)[\w\- ]*\.md$",
    re.I,
)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
             "build", "dist", "DerivedData", ".build", ".next", ".mypy_cache", ".ruff_cache",
             "xcuserdata"}  # Xcode rewrites xcuserdata when you only open a project
NEEDS_YOU = re.compile(r"needs bruce|needs (a )?decision|needs you|waiting on bruce|blocked|🚨", re.I)
UNCHECKED = re.compile(r"^\s*[-*]\s+\[ \]\s+(.*)")
CHECKED = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+")


# ---------------------------------------------------------------- config
def load_config() -> dict:
    cfg = tomllib.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if CONFIG_LOCAL.exists():  # one level of table merging is enough for this file
        for k, v in tomllib.loads(CONFIG_LOCAL.read_text()).items():
            both = isinstance(v, dict) and isinstance(cfg.get(k), dict)
            cfg[k] = {**cfg[k], **v} if both else v
    machine = os.environ.get("ORKA_MACHINE") or cfg.get("machine", "mba")
    machines = dict(cfg.get("machines", {}))
    if os.environ.get("ORKA_PEERS") is not None:
        machines = dict(x.split("=", 1) for x in os.environ["ORKA_PEERS"].split(",") if "=" in x)
    def p(env, key, default):
        return Path(os.path.expanduser(os.environ.get(env) or cfg.get(key) or default))
    th = cfg.get("thresholds", {})
    sv = cfg.get("serve", {})
    return {
        "port": int(os.environ.get("ORKA_PORT") or sv.get("port", 8765)),
        "refresh_s": int(sv.get("refresh_seconds", 30)),
        "machine": machine,
        "peers": {n: u for n, u in machines.items() if n != machine and u},  # other Macs' dashboards
        "machine_urls": machines,
        "alerts_on": bool(cfg.get("alerts", {}).get("enabled", True)),
        "alert_after_s": int(cfg.get("alerts", {}).get("after_seconds", 120)),
        "projects_root": p("ORKA_PROJECTS_ROOT", "projects_root", "~/dev/projects"),
        "claude_dir": p("ORKA_CLAUDE_DIR", "claude_projects_dir", "~/.claude/projects"),
        "state_dir": p("ORKA_STATE_DIR", "state_dir", "~/dev/.orka"),
        "active_s": int(th.get("active_minutes", 15)) * 60,
        "stale_s": int(th.get("stale_days", 14)) * 86400,
    }


def load_watchlist() -> list[dict]:
    if not WATCHLIST.exists():
        return []
    return tomllib.loads(WATCHLIST.read_text()).get("project", [])


def load_jobs() -> list[dict]:
    if not JOBS.exists():
        return []
    return tomllib.loads(JOBS.read_text()).get("job", [])


def resolve_path(entry: dict, root: Path) -> Path | None:
    if entry.get("path"):
        return Path(os.path.expanduser(entry["path"]))
    hits = sorted(p for p in root.glob(f"{entry['id']}-*") if p.is_dir())
    return hits[0] if hits else None


# ---------------------------------------------------------------- helpers
def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ago(ts: float | None, now: float) -> str:
    if not ts:
        return "never"
    d = max(0, now - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{int(d // n)}{unit} ago"
    return "just now"


def run(cmd: list[str], cwd: Path) -> str | None:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def short(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------- sensors
def git_info(path: Path, now: float) -> dict | None:
    if not (path / ".git").exists():
        return None
    g = ["git", "--no-optional-locks"]  # never take locks; live sessions may be using git
    last = run(g + ["log", "-1", "--format=%ct%x09%s"], path)
    last_ts, last_subj = (None, None)
    if last and "\t" in last:
        a, b = last.split("\t", 1)
        last_ts, last_subj = float(a), b
    porcelain = run(g + ["status", "--porcelain"], path) or ""
    since = run(g + ["rev-list", "--count", f"--since={int(now - 86400)}", "HEAD"], path)
    ab = run(g + ["rev-list", "--left-right", "--count", "@{u}...HEAD"], path)
    behind = ahead = None
    if ab and "\t" in ab:
        behind, ahead = (int(x) for x in ab.split("\t"))
    return {
        "branch": run(g + ["branch", "--show-current"], path),
        "dirty_files": len([l for l in porcelain.splitlines() if l.strip()]),
        "commits_24h": int(since) if since and since.isdigit() else 0,
        "last_commit": iso(last_ts),
        "last_commit_ts": last_ts,
        "last_commit_subject": short(last_subj or "", 100),
        "ahead": ahead,
        "behind": behind,
    }


def newest_mtime(path: Path, limit: int = 5000) -> float | None:
    newest, seen = None, 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.startswith("."):
                continue
            seen += 1
            try:
                m = os.stat(os.path.join(dirpath, f)).st_mtime
            except OSError:
                continue
            newest = m if newest is None or m > newest else newest
            if seen >= limit:
                return newest
    return newest


def plan_info(path: Path) -> dict:
    files = []
    for base in (path, path / "docs"):
        if base.is_dir():
            files += [f for f in base.iterdir() if f.is_file() and PLAN_NAMES.match(f.name)]
    out = {"files": [], "open_items": 0, "done_items": 0, "next_items": [], "needs_you": []}
    for f in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        open_ = [UNCHECKED.match(l).group(1) for l in lines if UNCHECKED.match(l)]
        done = sum(1 for l in lines if CHECKED.match(l))
        out["files"].append({"file": str(f.relative_to(path)), "open": len(open_),
                             "done": done, "modified": iso(f.stat().st_mtime)})
        out["open_items"] += len(open_)
        out["done_items"] += done
        for item in open_:
            if len(out["next_items"]) < 3:
                out["next_items"].append(short(item, 100))
        # Items that wait for Bruce: backlog rows or lines marked "Needs Bruce", 🚨, etc.
        for l in lines:
            if (CHECKED.match(l) or not NEEDS_YOU.search(l) or l.lstrip().startswith(("#", ">"))
                    or re.match(r"^\W*(marks|legend|status key|key)\b", l.strip(), re.I)):
                continue
            if l.strip().startswith("|"):  # table row: keep the first two cells (ID, topic)
                cells = [c.strip(" *") for c in l.strip().strip("|").split("|")]
                if not cells or set(cells[0]) <= set("-: "):
                    continue
                text = " · ".join(c for c in cells[:2] if c)
            else:
                text = re.sub(r"^\s*([-*]\s+(\[ \]\s+)?|\d+\.\s+)", "", l)
            item = {"file": f.name, "item": short(text.replace("**", ""), 110)}
            if item not in out["needs_you"]:
                out["needs_you"].append(item)
    return out


def encode_claude_dir(path: Path) -> str:
    # Claude Code names its log folder after the project path, with every
    # character that is not a letter or digit replaced by "-".
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def session_info(path: Path, claude_dir: Path, now: float) -> dict:
    out = {"available": claude_dir.is_dir(), "sessions_24h": 0, "last_activity": None,
           "last_activity_ts": None, "last_user_prompt": None, "last_claude_message": None,
           "todos": None, "pending_tool_call": False, "error": None}
    if not out["available"]:
        out["error"] = f"cannot read {claude_dir}"
        return out
    d = claude_dir / encode_claude_dir(path)
    logs = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True) if d.is_dir() else []
    out["sessions_24h"] = sum(1 for f in logs if now - f.stat().st_mtime < 86400)
    if not logs:
        return out
    newest = logs[0]
    try:
        with newest.open("rb") as fh:  # read only the tail; logs can be large
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()[1:] or []
    except OSError as e:
        out["error"] = str(e)
        return out
    last_ts, last_type, last_has_tool_use = None, None, False
    for line in tail:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = ev.get("type")
        ts = parse_iso(ev.get("timestamp"))
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if typ in ("user", "assistant") and ts:
            last_ts, last_type = ts, typ
            last_has_tool_use = typ == "assistant" and isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
        if typ == "user":
            t = _text_of(content)
            if t and not t.startswith("<") and not ev.get("isMeta"):
                out["last_user_prompt"] = short(t)
        elif typ == "assistant":
            t = _text_of(content)
            if t:
                out["last_claude_message"] = short(t, 240)
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "TodoWrite":
                        todos = (b.get("input") or {}).get("todos") or []
                        out["todos"] = {
                            "pending": sum(1 for t in todos if t.get("status") == "pending"),
                            "in_progress": [short(t.get("content", ""), 100) for t in todos
                                            if t.get("status") == "in_progress"],
                            "completed": sum(1 for t in todos if t.get("status") == "completed"),
                        }
    out["last_activity_ts"] = last_ts or newest.stat().st_mtime
    out["last_activity"] = iso(out["last_activity_ts"])
    # A tool call with no result yet often means Claude waits for permission.
    out["pending_tool_call"] = last_type == "assistant" and last_has_tool_use
    return out


def live_claude_cwds() -> list[str]:
    """Working folders of running `claude` processes (macOS/Linux, via ps + lsof)."""
    # `lsof -c claude` does not work: the kernel names the process after the
    # versioned binary (for example "2.1.330"). `ps` shows the name it was started as.
    ps = run(["ps", "-axo", "pid=,comm="], Path("/")) or ""
    pids = [p for p, _, c in (l.strip().partition(" ") for l in ps.splitlines())
            if os.path.basename(c.strip()) == "claude"]
    if not pids:
        return []
    out = run(["lsof", "-a", "-p", ",".join(pids), "-d", "cwd", "-Fn"], Path("/"))
    if not out:
        return []
    return [l[1:] for l in out.splitlines() if l.startswith("n")]


def tail_lines(path: Path, n: int = 5) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8000))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [short(l, 200) for l in lines if l.strip()][-n:]


def launchd_info(label: str, now: float) -> dict:
    out = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], Path("/"))
    if out is None:
        return {"status": "not loaded", "running": False}
    info: dict = {}
    for line in out.splitlines():
        if not line.startswith("\t") or line.startswith("\t\t"):
            continue  # top-level fields only; nested blocks repeat keys like "state"
        k, sep, v = line.strip().partition(" = ")
        if sep and k not in info:
            info[k] = v
    running = info.get("state") == "running"
    code = info.get("last exit code", "")
    log = Path(info["stdout path"]) if info.get("stdout path") else None
    last_run = log.stat().st_mtime if log and log.exists() else None
    failed = code not in ("", "0", "(never exited)")
    return {
        "status": "running" if running else ("failed" if failed else "ok"),
        "running": running, "pid": info.get("pid"), "last_exit_code": code or None,
        "runs": info.get("runs"), "program": info.get("program"),
        "last_run": iso(last_run), "last_run_ago": ago(last_run, now) if last_run else None,
        "log": str(log) if log else None, "stderr_log": info.get("stderr path"),
    }


def script_info(pattern: str) -> dict:
    ps = run(["ps", "-axo", "pid=,etime=,args="], Path("/")) or ""
    rx = re.compile(pattern)
    procs = []
    for line in ps.splitlines():
        pid, etime, args = (line.strip().split(None, 2) + ["", ""])[:3]
        if rx.search(args) and int(pid) != os.getpid():
            procs.append({"pid": pid, "elapsed": etime, "command": short(args, 120)})
    return {"status": "running" if procs else "stopped", "running": bool(procs), "processes": procs}


def job_info(job: dict, now: float) -> dict:
    rec = {k: job.get(k) for k in ("id", "kind", "machine", "label", "match", "alert_on_stop")}
    if job.get("kind") == "launchd":
        rec.update(launchd_info(job["label"], now))
    elif job.get("kind") == "script":
        rec.update(script_info(job["match"]))
    else:
        rec.update(status="unknown kind", running=False)
    log = job.get("log") or rec.get("log")
    if log:
        rec["log"] = os.path.expanduser(log)
        rec["log_tail"] = tail_lines(Path(rec["log"]))
    err = rec.get("stderr_log")
    if err:
        rec["stderr_tail"] = tail_lines(Path(err), 3)
    return rec


# ---------------------------------------------------------------- hook events (phase 2)
APPROVAL = re.compile(r"permission|approv", re.I)
SESSION_TTL = 48 * 3600  # forget a session after this long with no events (missed SessionEnd)
WORKING_TTL = 3600       # "working" with no event for this long is no longer trusted


EVENTS_MAX = 5_000_000  # rotate events.jsonl to events.1.jsonl beyond this size


def rotate_events(state_dir: Path) -> None:
    """Keep the log small. Renaming is safe while hooks write: each hook call
    opens the file by name, so the next event simply starts a new file."""
    f = state_dir / "events.jsonl"
    try:
        if f.stat().st_size > EVENTS_MAX:
            os.replace(f, state_dir / "events.1.jsonl")
    except OSError:
        pass


def read_events(state_dir: Path, max_bytes: int = 2_000_000) -> list[dict]:
    lines: list[str] = []
    for name in ("events.1.jsonl", "events.jsonl"):  # older file first
        f = state_dir / name
        if not f.is_file():
            continue
        with f.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            part = fh.read().decode("utf-8", errors="replace").splitlines()
        lines += part[1:] if size > max_bytes else part  # first line may be cut in half
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def hook_sessions(events: list[dict]) -> dict[str, dict]:
    """Latest state of each session, replayed from the event log.

    States: working (Claude is busy), needs_you (approval or question pending),
    ready (Claude finished; your turn), ended.
    """
    out: dict[str, dict] = {}
    for ev in events:
        sid, typ = ev.get("session_id"), ev.get("event")
        if not sid:
            continue
        s = out.setdefault(sid, {"id": sid, "state": "ready", "detail": "", "started": ev.get("ts")})
        s["cwd"] = ev.get("cwd") or s.get("cwd")
        s["last_ts"] = ev.get("ts")
        if typ == "SessionStart":
            s.update(state="ready", detail="session started")
        elif typ in ("UserPromptSubmit", "PostToolUse"):
            s.update(state="working", detail="Claude working")
        elif typ == "PermissionRequest":
            tool = ev.get("tool_name")
            s.update(state="needs_you", detail=f"approval needed: {tool}" if tool else "approval needed")
        elif typ == "StopFailure":
            s.update(state="ready", detail="Claude stopped with an error")
        elif typ == "Notification":
            ntype, msg = ev.get("notification_type") or "", ev.get("message") or ""
            if ntype in ("permission_prompt", "elicitation_dialog") or APPROVAL.search(msg):
                s.update(state="needs_you", detail=msg or "needs your approval")
            elif s["state"] != "needs_you":
                s.update(state="ready", detail=msg or "waiting for your input")
        elif typ == "Stop":
            s.update(state="ready", detail="Claude finished its reply")
        elif typ == "SessionEnd":
            s.update(state="ended", detail=ev.get("reason") or "session ended")
    return out


def project_sessions(path: Path, sessions: dict[str, dict], live: bool, now: float) -> list[dict]:
    """Open sessions whose folder is this project (or inside it)."""
    p = str(path)
    found = []
    for s in sessions.values():
        cwd = s.get("cwd") or ""
        if not (cwd == p or cwd.startswith(p + os.sep)):
            continue
        # No live claude process in the folder means its sessions are gone,
        # even if SessionEnd never arrived (closed terminal, crash).
        if s["state"] == "ended" or not live or now - (s.get("last_ts") or 0) > SESSION_TTL:
            continue
        if s["state"] == "working" and now - s["last_ts"] > WORKING_TTL:
            s = dict(s, state="ready", detail="no events for an hour")
        found.append(dict(s, last=iso(s["last_ts"]), last_ago=ago(s["last_ts"], now)))
    order = {"needs_you": 0, "working": 1, "ready": 2}
    return sorted(found, key=lambda s: (order.get(s["state"], 9), -(s["last_ts"] or 0)))


# ---------------------------------------------------------------- state
def decide_state(entry, live, git, sess, files_ts, cfg, now, hsess=None) -> tuple[str, float | None, str]:
    hsess = hsess or []
    acts = [t for t in (git and git["last_commit_ts"], sess["last_activity_ts"], files_ts,
                        *(s["last_ts"] for s in hsess)) if t]
    last = max(acts) if acts else None
    age = now - last if last else None
    if entry.get("status") == "paused":
        return "Paused", last, "on hold by you"
    if hsess:  # exact, from hooks
        if any(s["state"] == "needs_you" for s in hsess):
            s = next(s for s in hsess if s["state"] == "needs_you")
            return "Waiting on you", last, s["detail"]
        if any(s["state"] == "working" for s in hsess):
            return "Active", last, "Claude working"
        return "Idle", last, "Claude finished; your turn"
    if live and sess["pending_tool_call"] and age and age > 120:  # guess, for sessions started before hooks
        return "Waiting on you", last, "session open; last step is a tool call with no result (likely an approval)"
    if age is not None and age < cfg["active_s"]:
        return "Active", last, "recent activity"
    if live:
        return "Idle", last, "session open but quiet"
    if age is None or age > cfg["stale_s"]:
        return "Stale", last, "no activity for a long time"
    return "Idle", last, "no live session"


# ---------------------------------------------------------------- collect
def collect(cfg: dict) -> dict:
    now = time.time()
    cwds = live_claude_cwds()
    rotate_events(cfg["state_dir"])
    events = read_events(cfg["state_dir"])
    sessions = hook_sessions(events)
    projects = []
    for entry in load_watchlist():
        machines = entry.get("machine", cfg["machine"])
        machines = machines if isinstance(machines, list) else [machines]
        rec = {"id": entry["id"], "alias": entry.get("alias", []), "machine": machines,
               "watch_status": entry.get("status", "active")}
        if cfg["machine"] not in machines:
            rec.update(state="Remote", note=f"lives on {', '.join(machines)}; not read here")
            projects.append(rec)
            continue
        path = resolve_path(entry, cfg["projects_root"])
        if not path or not path.is_dir():
            rec.update(state="Missing", note=f"no folder {entry['id']}-* in {cfg['projects_root']}")
            projects.append(rec)
            continue
        live = any(c == str(path) or c.startswith(str(path) + os.sep) for c in cwds)
        git = git_info(path, now)
        files_ts = newest_mtime(path)
        sess = session_info(path, cfg["claude_dir"], now)
        hsess = project_sessions(path, sessions, live, now)
        state, last, why = decide_state(entry, live, git, sess, files_ts, cfg, now, hsess)
        rec.update(name=path.name, path=str(path), state=state, note=why,
                   live_session=live, last_activity=iso(last), last_activity_ago=ago(last, now),
                   git=git, plans=plan_info(path), session=sess, sessions=hsess,
                   newest_file=iso(files_ts))
        projects.append(rec)
    return {
        "generated_at": iso(now), "machine": cfg["machine"], "collector_version": VERSION,
        "self_check": {
            "session_logs_readable": cfg["claude_dir"].is_dir(),
            "live_session_detection": bool(cwds) or "no claude process found (or lsof missing)",
            "hook_events": (f"last event {ago(events[-1].get('ts'), now)}" if events
                            else "none yet (hooks not installed?)"),
        },
        "projects": projects,
        "jobs": [job_info(j, now) if j.get("machine", cfg["machine"]) == cfg["machine"]
                 else {"id": j.get("id"), "kind": j.get("kind"), "machine": j.get("machine"),
                       "status": "remote", "running": False}
                 for j in load_jobs()],
    }


def save(card: dict, cfg: dict, snapshot: bool = True) -> Path:
    sd = cfg["state_dir"]
    sd.mkdir(parents=True, exist_ok=True)
    out = sd / "scorecard.json"
    tmp = sd / "scorecard.json.tmp"
    tmp.write_text(json.dumps(card, indent=2, default=str))
    os.replace(tmp, out)  # atomic: Orka never reads a half-written file
    if not snapshot:
        return out
    db = sqlite3.connect(sd / "orka.db")
    db.execute("""CREATE TABLE IF NOT EXISTS snapshot(
        ts TEXT, id TEXT, state TEXT, last_activity TEXT, dirty INTEGER,
        commits_24h INTEGER, open_items INTEGER, live INTEGER)""")
    for p in card["projects"]:
        g = p.get("git") or {}
        db.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?,?,?)", (
            card["generated_at"], p["id"], p["state"], p.get("last_activity"),
            g.get("dirty_files"), g.get("commits_24h"),
            (p.get("plans") or {}).get("open_items"), int(bool(p.get("live_session")))))
    db.commit()
    db.close()
    return out


def show(card: dict) -> None:
    order = {"Waiting on you": 0, "Active": 1, "Idle": 2, "Stale": 3, "Paused": 4,
             "Missing": 5, "Remote": 6}
    print(f"Orka scorecard  {card['generated_at']}  machine={card['machine']}")
    for k, v in card["self_check"].items():
        print(f"  check: {k} = {v}")
    print()
    for p in sorted(card["projects"], key=lambda p: (order.get(p["state"], 9), p["id"])):
        g = p.get("git") or {}
        pl = p.get("plans") or {}
        line = f"{p['id']}  {p['state']:<15} {p.get('last_activity_ago', ''):<10}"
        if g:
            line += f" git:{g.get('commits_24h', 0)} commits/24h, {g.get('dirty_files', 0)} dirty"
        else:
            line += " git:none"
        if pl.get("files"):
            line += f" | plan: {pl['open_items']} open"
        print(line + f"  ({p.get('name', p.get('note', ''))})")
        if p["state"] == "Waiting on you":
            print(f"       waiting: {short(p.get('note', ''), 110)}")
        if len(p.get("sessions") or []) > 1:  # one line per agent when several share a project
            for s in p["sessions"]:
                print(f"       session {s['id'][:8]}: {s['state']:<9} {s['last_ago']:<10} {short(s['detail'], 80)}")
        if g.get("last_commit_subject"):
            print(f"       last commit: {g['last_commit_subject']}")
        if pl.get("needs_you"):
            print(f"       needs you ({len(pl['needs_you'])}): {pl['needs_you'][0]['item']}")
        if pl.get("next_items"):
            print(f"       next: {pl['next_items'][0]}")
        s = p.get("session") or {}
        if s.get("todos") and s["todos"]["in_progress"]:
            print(f"       doing: {s['todos']['in_progress'][0]}")
    if card.get("jobs"):
        print("\nJobs")
        for j in card["jobs"]:
            extra = (f"last run {j['last_run_ago']}" if j.get("last_run_ago")
                     else ", ".join(f"pid {p['pid']} up {p['elapsed']}" for p in j.get("processes", [])))
            code = f", exit {j['last_exit_code']}" if j.get("status") == "failed" else ""
            print(f"  {j['id']:<20} {j['status']:<10} {extra}{code}")


# ---------------------------------------------------------------- watch
def watch(args: list[str]) -> None:
    text = WATCHLIST.read_text() if WATCHLIST.exists() else ""
    entries = load_watchlist()
    if not args or args[0] == "list":
        for e in entries:
            print(f"{e['id']}  {e.get('status', 'active'):<7} {e.get('machine')}  {', '.join(e.get('alias', []))}")
        return
    cmd, pid = args[0], (args[1] if len(args) > 1 else "")
    if not re.fullmatch(r"\d{4}", pid):
        sys.exit("give a four-digit project id, for example: watch add 0099")
    block = re.compile(r"\n?\[\[project\]\]\nid\s*=\s*\"" + pid + r"\"\n(?:(?!\[\[project\]\]).*\n?)*")
    exists = any(e["id"] == pid for e in entries)
    if cmd == "add":
        if exists:
            sys.exit(f"{pid} is already on the watchlist")
        rest = args[2:]
        machine = "mba"
        if "--machine" in rest:
            i = rest.index("--machine")
            machine = rest[i + 1]
            rest = rest[:i] + rest[i + 2:]
        status = "paused" if "--paused" in rest else "active"
        aliases = [a for a in rest if not a.startswith("--")]
        text = text.rstrip("\n") + (
            f'\n\n[[project]]\nid      = "{pid}"\nalias   = {json.dumps(aliases)}\n'
            f'machine = "{machine}"\nstatus  = "{status}"\n')
    elif cmd == "remove":
        if not exists:
            sys.exit(f"{pid} is not on the watchlist")
        text = block.sub("\n", text).rstrip("\n") + "\n"
    elif cmd in ("pause", "resume"):
        if not exists:
            sys.exit(f"{pid} is not on the watchlist")
        new = "paused" if cmd == "pause" else "active"
        text = block.sub(lambda m: re.sub(r'status\s*=\s*"\w+"', f'status  = "{new}"', m.group(0)), text)
    else:
        sys.exit("watch commands: list, add, remove, pause, resume")
    WATCHLIST.write_text(text)
    tomllib.loads(text)  # fail loudly if the edit broke the file
    print(f"watch {cmd} {pid}: done")


# ---------------------------------------------------------------- new and clone
# Project numbers are one sequence across both Macs. The registry is watchlist.toml in
# Orka's own git repo: `new` pulls it, takes the next number, and pushes it straight
# back, so either Mac can start a project without the other one being on. Code moves
# between Macs only through GitHub (`clone`).
LAST_NUMBER = re.compile(r'^last_number\s*=\s*"(\d{4})"', re.M)


def sh(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a command; stop Orka with its error message if it fails."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        sys.exit(f"failed: {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def registry_repo() -> Path | None:
    """Orka's git repo if the watchlist is tracked in one with a remote (not in tests)."""
    top = run(["git", "rev-parse", "--show-toplevel"], WATCHLIST.parent)
    if top and run(["git", "remote"], WATCHLIST.parent) and \
            run(["git", "ls-files", "--error-unmatch", WATCHLIST.name], WATCHLIST.parent) is not None:
        return Path(top)
    return None


def registry_pull(apply: bool) -> Path | None:
    repo = registry_repo()
    if repo is None:
        print("  (watchlist is not in a git repo with a remote: no sync)")
    elif apply:
        sh(["git", "pull", "--ff-only", "-q"], repo)
    return repo


def registry_push(repo: Path | None, message: str) -> None:
    if repo is None:
        return
    sh(["git", "add", WATCHLIST.name], WATCHLIST.parent)
    sh(["git", "commit", "-q", "-m", message, "--", WATCHLIST.name], WATCHLIST.parent)
    sh(["git", "push", "-q"], repo)


def next_number(root: Path) -> str:
    text = WATCHLIST.read_text() if WATCHLIST.exists() else ""
    used = [int(e["id"]) for e in load_watchlist() if str(e.get("id", "")).isdigit()]
    used += [int(m) for m in LAST_NUMBER.findall(text)]
    used += [int(p.name[:4]) for p in root.glob("[0-9][0-9][0-9][0-9]-*")]
    return f"{max(used, default=0) + 1:04d}"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


def new_project(cfg: dict, args: list[str]) -> None:
    """orka.py new <name ...> [--machine mba] [--github] [--apply]"""
    apply, github = "--apply" in args, "--github" in args
    machine = cfg["machine"]
    if "--machine" in args:
        i = args.index("--machine")
        machine, args = args[i + 1], args[:i] + args[i + 2:]
    name = " ".join(a for a in args if not a.startswith("--")).strip()
    if not name or "/" in name:
        sys.exit("usage: orka.py new <project name> [--machine mba|mbp] [--github] [--apply]")
    root = cfg["projects_root"]
    print("Orka new project" + ("" if apply else " (preview; add --apply to do it)"))
    repo = registry_pull(apply)
    num = next_number(root)
    folder = root / f"{num}-{name}"
    print(f"  number:    {num}  (highest used so far +1, from the watchlist and {root})")
    print(f"  folder:    {folder}" + ("" if machine == cfg["machine"] else f"  (on {machine}, not created here)"))
    print(f"  watchlist: add {num} \"{name}\", machine {machine}" + (", then push" if repo else ""))
    if github:
        print(f"  GitHub:    new private repo {slug(name)}")
    if not apply:
        return
    url = ""
    if machine == cfg["machine"]:
        folder.mkdir(parents=True, exist_ok=False)
        sh(["git", "init", "-q"], folder)
        if github:
            sh(["gh", "repo", "create", slug(name), "--private", "--source", str(folder), "--remote", "origin"])
            url = sh(["git", "remote", "get-url", "origin"], folder)
    elif github:
        print("  (GitHub repo not created: run `gh repo create` on the Mac that holds the folder)")
    text = WATCHLIST.read_text() if WATCHLIST.exists() else ""
    if LAST_NUMBER.search(text):
        text = LAST_NUMBER.sub(f'last_number = "{num}"', text)
    else:  # must sit above the first [[project]] table
        i = text.find("[[project]]")
        i = len(text) if i < 0 else i
        text = text[:i] + f'last_number = "{num}"   # highest project number used on any Mac\n\n' + text[i:]
    text = text.rstrip("\n") + (
        f'\n\n[[project]]\nid      = "{num}"\nname    = {json.dumps(name)}\nalias   = []\n'
        f'machine = "{machine}"\nstatus  = "active"\n' + (f'repo    = "{url}"\n' if url else ""))
    tomllib.loads(text)  # fail loudly before writing a broken file
    WATCHLIST.write_text(text)
    registry_push(repo, f"New project {num}: {name}")
    print(f"  done{' and pushed' if repo else ''}")
    if folder.is_dir():
        print(folder)  # last line: the folder, for a shell function to cd into


def clone_project(cfg: dict, args: list[str]) -> None:
    """orka.py clone <id> [--apply]: copy a project from GitHub to the same folder name here."""
    apply = "--apply" in args
    pid = next((a for a in args if re.fullmatch(r"\d{4}", a)), None)
    if not pid:
        sys.exit("usage: orka.py clone <four-digit id> [--apply]")
    print("Orka clone" + ("" if apply else " (preview; add --apply to do it)"))
    repo = registry_pull(apply)
    entry = next((e for e in load_watchlist() if e.get("id") == pid), None)
    if not entry:
        sys.exit(f"{pid} is not on the watchlist")
    if not entry.get("repo") or not entry.get("name"):
        sys.exit(f"{pid} needs `name` and `repo` in watchlist.toml before it can be cloned")
    folder = cfg["projects_root"] / f"{pid}-{entry['name']}"
    machines = entry.get("machine", [])
    machines = machines if isinstance(machines, list) else [machines]
    print(f"  from:      {entry['repo']}")
    print(f"  to:        {folder}")
    if cfg["machine"] not in machines:
        print(f"  watchlist: machine {machines} -> {machines + [cfg['machine']]}" + (", then push" if repo else ""))
    if resolve_path(entry, cfg["projects_root"]):
        sys.exit(f"  stop: a {pid}-* folder already exists here")
    if not apply:
        return
    cfg["projects_root"].mkdir(parents=True, exist_ok=True)
    sh(["git", "clone", "-q", entry["repo"], str(folder)])
    if cfg["machine"] not in machines:
        new = json.dumps(machines + [cfg["machine"]])
        text = WATCHLIST.read_text()
        block = re.compile(r'(\[\[project\]\]\nid\s*=\s*"' + pid + r'"\n(?:(?!\[\[project\]\]).*\n?)*)')
        text = block.sub(lambda m: re.sub(r'machine\s*=\s*.*', f"machine = {new}", m.group(1), count=1), text)
        tomllib.loads(text)
        WATCHLIST.write_text(text)
        registry_push(repo, f"Clone project {pid} to {cfg['machine']}")
    print("  done")
    print(folder)


# ---------------------------------------------------------------- hook setup
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PermissionRequest",
               "Notification", "Stop", "StopFailure", "SessionEnd")


def install_hooks(apply: bool) -> None:
    """Add Orka's hook to ~/.claude/settings.json next to any hooks already there.

    Run it yourself: Claude is not allowed to change its own settings. Without
    --apply it only shows what it would add. It never changes or removes other hooks.
    """
    settings = Path.home() / ".claude" / "settings.json"
    text = settings.read_text() if settings.exists() else "{}"
    data = json.loads(text)
    cmd = f'/usr/bin/python3 "{HERE / "orka_hook.py"}"'
    hooks = data.setdefault("hooks", {})
    added = []
    for ev in HOOK_EVENTS:
        groups = hooks.setdefault(ev, [])
        if any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
            continue
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        added.append(ev)
    if not added:
        print(f"Orka hooks already installed in {settings}")
        return
    print(f"{'Adding' if apply else 'Would add'} to {settings}:\n  command: {cmd}\n  events:  {', '.join(added)}")
    if not apply:
        print("Nothing changed. Run again with --apply to install.")
        return
    backup = settings.with_name("settings.json.orka-backup")
    backup.write_text(text)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"Done. Backup of the old file: {backup}\nNew sessions report to Orka; restart open ones to include them.")


# ---------------------------------------------------------------- serve (phase 3)
# ---------------------------------------------------------------- other Macs (phase 4)
# Each Mac runs its own Orka. `serve` fetches the other Macs' /api/local over Tailscale and
# keeps the last good copy (in memory and in <state_dir>/peer-<name>.json). Either Mac may be
# off at any time: its data then stays on the dashboard, marked offline, until it answers again.
PEER_TIMEOUT = 5  # seconds per fetch


def fetch_peer(name: str, url: str) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/api/local",
                                 headers={"User-Agent": f"orka/{VERSION}"})
    with urllib.request.urlopen(req, timeout=PEER_TIMEOUT) as r:
        card = json.load(r)
    if not isinstance(card, dict) or not isinstance(card.get("projects"), list):
        raise ValueError("answer is not an Orka scorecard")
    if card.get("machine") != name:
        raise ValueError(f"answered as machine {card.get('machine')!r}, expected {name!r}")
    return card


def peer_offline_after(cfg: dict) -> int:
    return max(90, 3 * cfg["refresh_s"])


def merge_peers(card: dict, peers: dict[str, dict], cfg: dict, now: float) -> dict:
    """This Mac's card, with each Remote project or job replaced by the other Mac's record.
    A project that lives on this Mac and another one keeps this Mac's record and gets the
    other Mac's copy under `elsewhere` (a short summary, tagged like a peer record)."""
    status, projects, jobs, copies = [], {}, {}, {}
    for name, ps in sorted(peers.items()):
        last_ok = ps.get("last_ok")
        online = bool(last_ok) and now - last_ok < peer_offline_after(cfg)
        status.append({"machine": name, "online": online, "last_seen": iso(last_ok),
                       "last_seen_ago": ago(last_ok, now) if last_ok else None,
                       "error": None if online else ps.get("error")})
        pc = ps.get("card")
        if not pc:
            continue
        tag = {"from_machine": name, "as_of": pc.get("generated_at"), "peer_offline": not online}
        for p in pc.get("projects", []):
            if p.get("state") != "Remote":
                projects.setdefault(p.get("id"), dict(p, **tag))
                copies.setdefault(p.get("id"), []).append(dict(
                    {k: p.get(k) for k in ("state", "note", "last_activity_ago")}, **tag))
        for j in pc.get("jobs", []):
            if j.get("status") != "remote":
                jobs.setdefault(j.get("id"), dict(j, **tag))
    out = dict(card, peers=status)
    out["projects"] = [projects.get(p["id"], p) if p.get("state") == "Remote"
                       else dict(p, elsewhere=copies[p["id"]]) if p["id"] in copies else p
                       for p in card["projects"]]
    out["jobs"] = [jobs.get(j.get("id"), j) if j.get("status") == "remote" else j
                   for j in card.get("jobs", [])]
    return out


def peer_loop(name: str, url: str, cfg: dict, peers: dict, lock: threading.Lock,
              wake: threading.Event) -> None:
    cache = cfg["state_dir"] / f"peer-{name}.json"
    while True:
        try:
            pc = fetch_peer(name, url)
            t = time.time()
            with lock:
                peers[name].update(card=pc, last_ok=t, error=None)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"url": url, "last_ok": t, "card": pc}, default=str))
            os.replace(tmp, cache)
        except Exception as e:  # off, asleep, or not set up yet: keep the last copy
            with lock:
                peers[name]["error"] = f"{type(e).__name__}: {e}"
            wake.wait(min(10, cfg["refresh_s"]))  # retry sooner, so a Mac that wakes shows up fast
            wake.clear()
            continue
        wake.wait(cfg["refresh_s"])  # set early after an action, to show its result sooner
        wake.clear()


# ---------------------------------------------------------------- phone alerts (phase 4)
# Pushover push messages for "Waiting on you" and failed jobs. Codes live in
# <state_dir>/pushover.toml (private, not in git). Only one Mac sends: the first machine
# name, in sorted order, among this Mac and the other Macs that are online (so the MBA,
# unless it is off). A condition must last alert_after_s before it alerts, and alerts once.
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def pushover_codes(cfg: dict) -> dict | None:
    try:
        c = tomllib.loads((cfg["state_dir"] / "pushover.toml").read_text())
    except (OSError, ValueError):
        return None
    ok = all(re.fullmatch(r"[A-Za-z0-9]{30}", str(c.get(k, ""))) for k in ("user_key", "api_token"))
    return c if ok else None


def send_pushover(codes: dict, title: str, message: str, url: str | None = None) -> None:
    fields = {"token": codes["api_token"], "user": codes["user_key"],
              "title": title[:250], "message": message[:1000] or title[:250]}
    if url:
        fields.update(url=url, url_title="Open Orka")
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(urllib.request.Request(PUSHOVER_URL, data=data), timeout=10) as r:
        if json.load(r).get("status") != 1:
            raise RuntimeError("Pushover did not accept the message")


def is_alert_sender(cfg: dict, merged: dict) -> bool:
    online = [cfg["machine"]] + [m["machine"] for m in merged.get("peers", []) if m["online"]]
    return sorted(online)[0] == cfg["machine"]


def stopped_jobs(merged: dict, was_running: set) -> tuple[set, set]:
    """Script jobs with `alert_on_stop` that Orka saw running and that are gone now.
    A process that ends can't tell us whether it finished or crashed, so the alert says so.
    Returns (stopped ids, updated was_running). An offline Mac's jobs keep their mark, so a
    run that died with a shutdown alerts when that Mac is back."""
    stopped, keep = set(), set(was_running)
    for j in merged.get("jobs", []):
        if not j.get("alert_on_stop") or j.get("peer_offline") or j.get("status") == "remote":
            continue
        if j.get("running"):
            keep.add(j["id"])
        elif j["id"] in keep:
            stopped.add(j["id"])
    return stopped, keep


def alert_conditions(merged: dict, stopped: set = frozenset()) -> dict[str, tuple[str, str]]:
    """key -> (title, message) for everything that should alert right now."""
    out = {}
    for p in merged.get("projects", []):
        name = re.sub(r"^\d{4}-", "", p.get("name") or "")
        for c in [p] + p.get("elsewhere", []):  # a project on two Macs can wait on you on either
            if c.get("state") == "Waiting on you" and not c.get("peer_offline"):
                mach = c.get("from_machine") or merged.get("machine")
                key = f"wait:{p['id']}" if c is p or not p.get("elsewhere") else f"wait:{p['id']}:{mach}"
                out[key] = (f"{p['id']} {name} is waiting on you ({mach})", short(c.get("note") or "", 200))
    for j in merged.get("jobs", []):
        mach = j.get("from_machine") or merged.get("machine")
        if j.get("status") in ("failed", "not loaded") and not j.get("peer_offline"):
            code = f", exit code {j.get('last_exit_code')}" if j.get("status") == "failed" else ""
            out[f"job:{j.get('id')}"] = (f"Job {j.get('id')} {j.get('status')} ({mach})", f"{j.get('status')}{code}")
        elif j.get("id") in stopped:
            out[f"stop:{j['id']}"] = (f"Job {j['id']} stopped ({mach})",
                                      "No longer running: finished or crashed. Check its log.")
    return out


def alerts_due(conds: dict, seen: dict, now: float, after_s: int) -> tuple[list[str], dict]:
    """seen: key -> {"since": ts, "sent": bool}. Returns keys to send now, and the new seen."""
    new = {k: seen.get(k, {"since": now, "sent": False}) for k in conds}  # cleared keys drop out
    due = [k for k, v in new.items() if not v["sent"] and now - v["since"] >= after_s]
    return due, new


# ---------------------------------------------------------------- controls (phase 5)
# POST /api/action {"action": "pause"|"resume", "project": "0071"}. The Mac that owns the
# project does it (others forward the request), edits watchlist.toml, commits and pushes.
# Allowed: requests via `tailscale serve` whose Tailscale-User-Login is listed in
# <state_dir>/controls.toml (private: the repo is public), or plain local requests to
# 127.0.0.1. Browsers can't send cross-site JSON POSTs without a preflight we never answer,
# and Host/Origin must be ours. Every attempt is logged to <state_dir>/actions.jsonl.
ACTIONS = ("pause", "resume")
WATCH_LOCK = threading.Lock()


def allowed_logins(cfg: dict) -> list[str]:
    try:
        return list(tomllib.loads((cfg["state_dir"] / "controls.toml").read_text()).get("allowed_logins", []))
    except (OSError, ValueError):
        return []


def log_action(cfg: dict, rec: dict) -> None:
    with open(cfg["state_dir"] / "actions.jsonl", "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def project_owner(cfg: dict, pid: str) -> str | None:
    """The Mac that should carry out an action on this project: this Mac if it has it."""
    entry = next((e for e in load_watchlist() if e.get("id") == pid), None)
    if entry is None:
        return None
    machines = entry.get("machine", cfg["machine"])
    machines = machines if isinstance(machines, list) else [machines]
    if cfg["machine"] in machines:
        return cfg["machine"]
    return next((m for m in machines if m in cfg["peers"]), None)


def set_watch_status(pid: str, action: str) -> str:
    """Pause or resume a project in watchlist.toml, then commit and push it."""
    with WATCH_LOCK:
        repo = registry_repo()
        if repo:
            sh(["git", "pull", "--ff-only", "-q"], repo)
        entry = next((e for e in load_watchlist() if e.get("id") == pid), None)
        if entry is None:
            raise ValueError(f"{pid} is not on the watchlist")
        want = "paused" if action == "pause" else "active"
        if entry.get("status", "active") == want:
            return f"already {want}"
        watch([action, pid])
        if repo:
            try:
                registry_push(repo, f"{action.title()} project {pid} (from the dashboard)")
            except SystemExit:  # the other Mac pushed first: rebase on top and push again
                sh(["git", "pull", "--rebase", "-q"], repo)
                sh(["git", "push", "-q"], repo)
        return want


def forward_action(url: str, body: dict) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/api/action", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"orka/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


SNAPSHOT_EVERY = 600  # seconds between orka.db history rows while serving


def serve(cfg: dict) -> None:
    """Dashboard + JSON API on 127.0.0.1 only. Controls: POST /api/action (phase 5).

    Reach it from other devices with `tailscale serve --bg <port>`, which adds
    HTTPS and lets only your own tailnet devices in.
    """
    state = {"card": None, "error": None}
    lock = threading.Lock()
    peers: dict[str, dict] = {}
    cfg["state_dir"].mkdir(parents=True, exist_ok=True)
    for name, url in cfg["peers"].items():
        try:  # last known copy from before a restart
            saved = json.loads((cfg["state_dir"] / f"peer-{name}.json").read_text())
            saved = saved if saved.get("url") == url else {}
        except (OSError, ValueError):
            saved = {}
        peers[name] = {"card": saved.get("card"), "last_ok": saved.get("last_ok"), "error": None}
    wake_collect = threading.Event()
    wake_peer = {name: threading.Event() for name in cfg["peers"]}

    def refresher() -> None:
        last_snap = 0.0
        while True:
            try:
                card = collect(cfg)
                snap = time.time() - last_snap > SNAPSHOT_EVERY
                save(card, cfg, snapshot=snap)
                last_snap = time.time() if snap else last_snap
                with lock:
                    state.update(card=card, error=None)
                    snap_peers = {n: dict(p) for n, p in peers.items()}
                check_alerts(merge_peers(card, snap_peers, cfg, time.time()))
            except Exception as e:  # keep serving the last good card
                with lock:
                    state["error"] = f"{type(e).__name__}: {e}"
            wake_collect.wait(cfg["refresh_s"])  # set early after an action
            wake_collect.clear()

    alert_file = cfg["state_dir"] / "alerts.json"
    try:
        alert_seen = json.loads(alert_file.read_text())
    except (OSError, ValueError):
        alert_seen = {}
    runs_file = cfg["state_dir"] / "job-runs.json"  # alert_on_stop jobs seen running
    try:
        was_running = set(json.loads(runs_file.read_text()))
    except (OSError, ValueError):
        was_running = set()

    def check_alerts(merged: dict) -> None:
        nonlocal alert_seen, was_running
        codes = pushover_codes(cfg) if cfg["alerts_on"] else None
        if not codes:
            state["alerts"] = "off" if not cfg["alerts_on"] else "no Pushover codes"
            return
        sender = is_alert_sender(cfg, merged)
        stopped, was_running = stopped_jobs(merged, was_running)
        conds = alert_conditions(merged, stopped)
        due, alert_seen = alerts_due(conds, alert_seen, time.time(), cfg["alert_after_s"])
        state["alerts"] = "on (this Mac sends)" if sender else "on (another Mac sends)"
        for k in due:
            if sender:
                try:
                    send_pushover(codes, *conds[k], url=cfg["machine_urls"].get(cfg["machine"]) or None)
                except Exception as e:
                    state["alerts"] = f"send failed: {type(e).__name__}: {e}"
                    continue  # retry next cycle
            alert_seen[k]["sent"] = True  # the other Mac's sender handles it if not us
            if k.startswith("stop:"):
                was_running.discard(k[5:])  # told once; it alerts again only after a new run
        for f, data in ((alert_file, alert_seen), (runs_file, sorted(was_running))):
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, f)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            elif path in ("/api/scorecard", "/api/local"):
                # /api/local: this Mac only (what other Macs fetch). /api/scorecard: all Macs.
                with lock:
                    card, err = state["card"], state["error"]
                    snap = {n: dict(p) for n, p in peers.items()}
                if card is None:
                    self._send(503, json.dumps({"error": err or "first collect still running"}).encode(),
                               "application/json")
                    return
                if path == "/api/scorecard":
                    card = merge_peers(card, snap, cfg, time.time())
                body = dict(card, serve_error=err, viewer=self.headers.get("Tailscale-User-Login"),
                            alerts=state.get("alerts"))
                self._send(200, json.dumps(body, default=str).encode(), "application/json")
            elif path == "/api/health":
                self._send(200, b'{"ok": true}', "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/api/action":
                return self._send(404, b"not found", "text/plain")
            login = self.headers.get("Tailscale-User-Login")
            rec = {"ts": iso(time.time()), "machine": cfg["machine"], "viewer": login or "local"}
            try:
                body = self._check_and_read(login)
                rec.update(action=body["action"], project=body["project"], via=body.get("via"))
                rec["result"] = self._act(body)
                code, out = 200, {"ok": True, **rec}
            except PermissionError as e:
                rec["error"] = str(e)
                code, out = 403, {"ok": False, "error": str(e)}
            except (ValueError, KeyError) as e:
                rec["error"] = str(e)
                code, out = 400, {"ok": False, "error": str(e)}
            except (Exception, SystemExit) as e:  # git or the other Mac failed
                rec["error"] = f"{type(e).__name__}: {e}"
                code, out = 500, {"ok": False, "error": rec["error"]}
            log_action(cfg, rec)
            self._send(code, json.dumps(out, default=str).encode(), "application/json")

        def _check_and_read(self, login: str | None) -> dict:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            local = host in ("127.0.0.1", "localhost")
            ours = local or host in {urllib.parse.urlparse(u).hostname
                                     for u in cfg["machine_urls"].values() if u}
            if not ours:
                raise PermissionError(f"unexpected Host {host!r}")
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).hostname != host:
                raise PermissionError("cross-site request")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise PermissionError("Content-Type must be application/json")
            if login is not None:
                if login not in allowed_logins(cfg):
                    raise PermissionError("this Tailscale login may not use controls "
                                          f"(see {cfg['state_dir'] / 'controls.toml'})")
            elif not local:
                raise PermissionError("no Tailscale login on a remote request")
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 10_000:
                raise ValueError("missing or oversized body")
            body = json.loads(self.rfile.read(n))
            if body.get("action") not in ACTIONS or not re.fullmatch(r"\d{4}", str(body.get("project"))):
                raise ValueError(f"action must be one of {ACTIONS}, project a four-digit id")
            return body

        def _act(self, body: dict) -> str:
            pid, owner = body["project"], project_owner(cfg, body["project"])
            if owner == cfg["machine"]:
                result = set_watch_status(pid, body["action"])
                wake_collect.set()
                return f"{pid} {result} on {owner}"
            if owner is None:
                raise ValueError(f"{pid} is not on the watchlist, or lives on a Mac Orka can't reach")
            if body.get("via"):
                raise ValueError("already forwarded once; the Macs disagree about the watchlist")
            with lock:
                last_ok = peers[owner].get("last_ok")
            if not last_ok or time.time() - last_ok > peer_offline_after(cfg):
                raise ValueError(f"{owner} is offline; try again when it is on")
            r = forward_action(cfg["peers"][owner], dict(body, via=cfg["machine"]))
            if not r.get("ok"):
                raise RuntimeError(f"{owner}: {r.get('error')}")
            wake_peer[owner].set()
            return r.get("result", "done")

        def log_message(self, *args) -> None:  # quiet
            pass

    threading.Thread(target=refresher, daemon=True).start()
    for name, url in cfg["peers"].items():
        threading.Thread(target=peer_loop, args=(name, url, cfg, peers, lock, wake_peer[name]),
                         daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), Handler)
    print(f"Orka dashboard on http://127.0.0.1:{cfg['port']}  machine={cfg['machine']}  "
          f"peers={', '.join(cfg['peers']) or 'none'}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def install_agent(cfg: dict, apply: bool) -> None:
    """Run `orka.py serve` at login and keep it running, as a launchd agent.

    Run it yourself (it writes to ~/Library/LaunchAgents). Without --apply it only shows the plan.
    """
    import plistlib
    import shutil
    label = f"com.{os.environ.get('USER', 'user')}.orka"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv not found on PATH")
    log = cfg["state_dir"] / "serve.log"
    spec = {
        "Label": label,
        "ProgramArguments": [uv, "run", str(HERE / "orka.py"), "serve"],
        "WorkingDirectory": str(HERE),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    domain = f"gui/{os.getuid()}"
    print(f"{'Installing' if apply else 'Would install'} launchd agent {label}\n"
          f"  file:    {plist}\n  runs:    {' '.join(spec['ProgramArguments'])}\n"
          f"  log:     {log}\n  then:    launchctl bootstrap {domain} {plist}")
    if not apply:
        print("Nothing changed. Run again with --apply to install.")
        return
    cfg["state_dir"].mkdir(parents=True, exist_ok=True)
    run(["launchctl", "bootout", f"{domain}/{label}"], Path("/"))  # if already loaded
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(spec))
    r = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    print(f"Done. Dashboard: http://127.0.0.1:{cfg['port']}\n"
          f"For your phone: tailscale serve --bg {cfg['port']}")


def show_events(cfg: dict, n: int) -> None:
    for ev in read_events(cfg["state_dir"])[-n:]:
        when = datetime.fromtimestamp(ev.get("ts", 0)).strftime("%m-%d %H:%M:%S")
        folder = os.path.basename(ev.get("cwd") or "")
        extra = ev.get("message") or ev.get("tool_name") or ev.get("source") or ev.get("reason") or ""
        print(f"{when}  {ev.get('event', '?'):<17} {(ev.get('session_id') or '')[:8]}  {folder:<28.28}  {extra}")


def main() -> None:
    args = sys.argv[1:] or ["collect"]
    if args[0] == "watch":
        return watch(args[1:])
    if args[0] == "install-hooks":
        return install_hooks("--apply" in args)
    cfg = load_config()
    if args[0] == "events":
        return show_events(cfg, int(args[1]) if len(args) > 1 else 30)
    if args[0] == "serve":
        return serve(cfg)
    if args[0] == "install-agent":
        return install_agent(cfg, "--apply" in args)
    if args[0] == "alert-test":
        codes = pushover_codes(cfg)
        if not codes:
            sys.exit(f"no valid codes in {cfg['state_dir'] / 'pushover.toml'}")
        if "--apply" not in args:
            return print("Would send one test message to your phone. Add --apply to send it.")
        send_pushover(codes, f"Orka test from {cfg['machine']}", "Phone alerts work.",
                      url=cfg["machine_urls"].get(cfg["machine"]) or None)
        return print("sent")
    if args[0] == "new":
        return new_project(cfg, args[1:])
    if args[0] == "clone":
        return clone_project(cfg, args[1:])
    card = collect(cfg)
    out = save(card, cfg)
    if args[0] == "show":
        show(card)
    else:
        print(f"wrote {out} ({len(card['projects'])} projects)")


if __name__ == "__main__":
    main()
