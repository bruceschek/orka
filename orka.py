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
  uv run orka.py install-hooks [--apply]  # add Orka's hook to ~/.claude/settings.json

The collector uses no AI and no tokens. It only reads files, git and processes.
Environment overrides (for testing): ORKA_PROJECTS_ROOT, ORKA_CLAUDE_DIR, ORKA_STATE_DIR.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

VERSION = "0.2.0"
HERE = Path(__file__).resolve().parent
CONFIG = HERE / "orka.toml"
WATCHLIST = HERE / "watchlist.toml"

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
    def p(env, key, default):
        return Path(os.path.expanduser(os.environ.get(env) or cfg.get(key) or default))
    th = cfg.get("thresholds", {})
    return {
        "machine": cfg.get("machine", "mba"),
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


# ---------------------------------------------------------------- hook events (phase 2)
APPROVAL = re.compile(r"permission|approv", re.I)
SESSION_TTL = 48 * 3600  # forget a session after this long with no events (missed SessionEnd)
WORKING_TTL = 3600       # "working" with no event for this long is no longer trusted


def read_events(state_dir: Path, max_bytes: int = 2_000_000) -> list[dict]:
    f = state_dir / "events.jsonl"
    if not f.is_file():
        return []
    with f.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        lines = fh.read().decode("utf-8", errors="replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]  # first line may be cut in half
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
    }


def save(card: dict, cfg: dict) -> Path:
    sd = cfg["state_dir"]
    sd.mkdir(parents=True, exist_ok=True)
    out = sd / "scorecard.json"
    tmp = sd / "scorecard.json.tmp"
    tmp.write_text(json.dumps(card, indent=2, default=str))
    os.replace(tmp, out)  # atomic: Orka never reads a half-written file
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
    card = collect(cfg)
    out = save(card, cfg)
    if args[0] == "show":
        show(card)
    else:
        print(f"wrote {out} ({len(card['projects'])} projects)")


if __name__ == "__main__":
    main()
