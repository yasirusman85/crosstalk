#!/usr/bin/env python3
"""crosstalk: a live shared brain for parallel Claude Code sessions.

Every Claude Code session in the same project writes to one local SQLite
database (WAL mode, so concurrent writers are safe). Hooks feed each session
what the *other* live sessions are doing: their goals, the files they are
editing, and the decisions they broadcast. Edits to a file another live
session touched recently are flagged before they happen.

Standard library only. No network, no telemetry; everything stays on disk.

Usage:
  crosstalk.py hook <event>       (called by Claude Code hooks, reads JSON on stdin)
  crosstalk.py status             show live sessions and recent activity
  crosstalk.py note <text...>     broadcast a decision/note to the other sessions
  crosstalk.py log [N]            show the last N events (default 30)
  crosstalk.py forget             wipe this project's shared brain

Environment: CROSSTALK_MODE (warn|ask|block|off), CROSSTALK_CLASH_MINUTES,
CROSSTALK_LIVE_MINUTES, CROSSTALK_HOME, CROSSTALK_PROJECT (force a shared project root).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("CROSSTALK_HOME", Path.home() / ".crosstalk"))
LIVE_SECONDS = int(os.environ.get("CROSSTALK_LIVE_MINUTES", "20")) * 60
CLASH_SECONDS = int(os.environ.get("CROSSTALK_CLASH_MINUTES", "30")) * 60
# warn: tell Claude + user and continue. ask: require a confirmation. block: refuse. off: no checks.
MODE = os.environ.get("CROSSTALK_MODE", "warn").lower()
MAX_DIGEST = 12
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  cwd TEXT,
  started REAL NOT NULL,
  last_seen REAL NOT NULL,
  ended REAL,
  goal TEXT,
  cursor INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,          -- start | goal | edit | note | end
  file TEXT,
  text TEXT
);
CREATE INDEX IF NOT EXISTS events_file ON events(file, ts);
"""

ANIMALS = [
    "otter", "falcon", "lynx", "heron", "badger", "orca", "raven", "ibex",
    "gecko", "puffin", "marten", "bison", "koala", "mantis", "osprey", "tapir",
]


# ---------------------------------------------------------------- storage

def project_root(cwd: str | None) -> Path:
    forced = os.environ.get("CROSSTALK_PROJECT")
    if forced:
        return Path(forced).expanduser().resolve()
    cwd = cwd or os.getcwd()
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).resolve()


def connect(root: Path) -> sqlite3.Connection:
    key = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    d = HOME / f"{root.name}-{key}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.txt").write_text(str(root) + "\n")
    db = sqlite3.connect(d / "brain.db", timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript(SCHEMA)
    return db


def label_for(session_id: str) -> str:
    n = int(hashlib.sha1(session_id.encode()).hexdigest(), 16)
    return f"{ANIMALS[n % len(ANIMALS)]}-{session_id[:4]}"


def rel(root: Path, path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    try:
        return str(p.resolve().relative_to(root))
    except (ValueError, OSError):
        return str(p)


def ago(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def touch(db, sid: str, cwd: str | None) -> sqlite3.Row:
    now = time.time()
    row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        # Start the cursor at "now" so a new session isn't flooded by history;
        # the SessionStart digest gives it the summary instead.
        top = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        db.execute(
            "INSERT INTO sessions(id,label,cwd,started,last_seen,cursor) VALUES(?,?,?,?,?,?)",
            (sid, label_for(sid), cwd, now, now, top),
        )
    else:
        db.execute("UPDATE sessions SET last_seen=?, ended=NULL WHERE id=?", (now, sid))
    return db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()


def log(db, sid: str, kind: str, file: str | None = None, text: str | None = None) -> None:
    db.execute(
        "INSERT INTO events(ts,session_id,kind,file,text) VALUES(?,?,?,?,?)",
        (time.time(), sid, kind, file, text),
    )


def live_others(db, sid: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM sessions WHERE id<>? AND ended IS NULL AND last_seen>? ORDER BY last_seen DESC",
        (sid, time.time() - LIVE_SECONDS),
    ).fetchall()


def files_of(db, sid: str, limit: int = 5) -> list[str]:
    rows = db.execute(
        "SELECT file, MAX(ts) t FROM events WHERE session_id=? AND kind='edit' "
        "GROUP BY file ORDER BY t DESC LIMIT ?", (sid, limit),
    ).fetchall()
    return [r["file"] for r in rows]


def describe(db, s: sqlite3.Row) -> str:
    line = f"- {s['label']} (active {ago(s['last_seen'])})"
    if s["goal"]:
        line += f": working on \"{s['goal']}\""
    files = files_of(db, s["id"])
    if files:
        line += f"\n    editing: {', '.join(files)}"
    return line


def fmt_event(e: sqlite3.Row, labels: dict[str, str]) -> str:
    who = labels.get(e["session_id"], e["session_id"][:8])
    if e["kind"] == "edit":
        return f"- {who} edited {e['file']} ({ago(e['ts'])})"
    if e["kind"] == "note":
        return f"- {who} NOTE: {e['text']} ({ago(e['ts'])})"
    if e["kind"] == "goal":
        return f"- {who} started: \"{e['text']}\" ({ago(e['ts'])})"
    if e["kind"] == "start":
        return f"- {who} joined ({ago(e['ts'])})"
    if e["kind"] == "end":
        return f"- {who} left ({ago(e['ts'])})"
    return f"- {who} {e['kind']} ({ago(e['ts'])})"


def labels_map(db) -> dict[str, str]:
    return {r["id"]: r["label"] for r in db.execute("SELECT id,label FROM sessions")}


def recent_notes(db, limit: int = 8) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM events WHERE kind='note' ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()[::-1]


# ---------------------------------------------------------------- hooks

def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))


def context(event: str, text: str, system: str | None = None) -> None:
    out: dict = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if system:
        out["systemMessage"] = system
    emit(out)


def on_session_start(db, root, sid, data) -> None:
    me = touch(db, sid, data.get("cwd"))
    log(db, sid, "start")
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:  # lets `crosstalk.py note` from the Bash tool know who is talking
        with open(env_file, "a") as f:
            f.write(f"export CROSSTALK_SESSION={sid}\nexport CROSSTALK_PROJECT={root}\n")
    others = live_others(db, sid)
    notes = recent_notes(db)
    if not others and not notes:
        return
    labels = labels_map(db)
    parts = [f"[crosstalk] You are session {me['label']}. This project has a shared brain "
             "with the other Claude Code sessions working in it."]
    if others:
        parts.append(f"{len(others)} other live session(s):")
        parts += [describe(db, s) for s in others]
        parts.append("Avoid editing files they are working on unless the user asks; "
                     "coordinate through the user.")
    if notes:
        parts.append("Shared notes and decisions:")
        parts += [fmt_event(n, labels) for n in notes]
    parts.append("To share a decision, convention or warning with the other sessions, run: "
                 f"python3 \"{Path(__file__).resolve()}\" note \"<one line>\"")
    banner = None
    if others:
        banner = f"crosstalk: {len(others)} other live session(s) here: " + \
                 ", ".join(s["label"] for s in others)
    context("SessionStart", "\n".join(parts), banner)


def on_prompt(db, root, sid, data) -> None:
    me = touch(db, sid, data.get("cwd"))
    prompt = " ".join((data.get("prompt") or "").split())
    if prompt and not prompt.startswith("/"):
        goal = prompt[:140] + ("…" if len(prompt) > 140 else "")
        db.execute("UPDATE sessions SET goal=? WHERE id=?", (goal, sid))
        log(db, sid, "goal", text=goal)
    news = db.execute(
        "SELECT * FROM events WHERE id>? AND session_id<>? AND kind IN ('edit','note','goal','end') "
        "ORDER BY id", (me["cursor"], sid),
    ).fetchall()
    top = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    db.execute("UPDATE sessions SET cursor=? WHERE id=?", (top, sid))
    if not news:
        return
    # Collapse repeated edits of the same file into one line.
    seen, lines = set(), []
    labels = labels_map(db)
    for e in reversed(news):
        k = (e["session_id"], e["kind"], e["file"] if e["kind"] == "edit" else e["id"])
        if k in seen:
            continue
        seen.add(k)
        lines.append(fmt_event(e, labels))
    lines = lines[:MAX_DIGEST][::-1]
    context("UserPromptSubmit",
            "[crosstalk] Since your last turn, other sessions in this project:\n" + "\n".join(lines))


def on_pre_edit(db, root, sid, data) -> None:
    if MODE == "off" or data.get("tool_name") not in EDIT_TOOLS:
        return
    touch(db, sid, data.get("cwd"))
    ti = data.get("tool_input") or {}
    f = rel(root, ti.get("file_path") or ti.get("notebook_path"))
    if not f:
        return
    rows = db.execute(
        "SELECT s.label, s.goal, MAX(e.ts) t FROM events e JOIN sessions s ON s.id=e.session_id "
        "WHERE e.file=? AND e.kind='edit' AND e.session_id<>? AND e.ts>? "
        "AND s.ended IS NULL AND s.last_seen>? GROUP BY s.id",
        (f, sid, time.time() - CLASH_SECONDS, time.time() - LIVE_SECONDS),
    ).fetchall()
    if not rows:
        return
    who = "; ".join(
        f"{r['label']} edited it {ago(r['t'])}" + (f" while working on \"{r['goal']}\"" if r["goal"] else "")
        for r in rows
    )
    reason = (f"crosstalk: {f} is being edited by another live session ({who}). "
              "Re-read the file before changing it, keep the change minimal, "
              "and tell the user about the overlap.")
    hso: dict = {"hookEventName": "PreToolUse"}
    if MODE == "block":
        hso.update(permissionDecision="deny", permissionDecisionReason=reason)
    elif MODE == "ask":
        hso.update(permissionDecision="ask", permissionDecisionReason=reason)
    else:
        hso.update(additionalContext=reason)
    emit({"hookSpecificOutput": hso, "systemMessage": f"⚠ crosstalk: {f} overlaps with {who}"})


def on_post_edit(db, root, sid, data) -> None:
    if data.get("tool_name") not in EDIT_TOOLS:
        return
    touch(db, sid, data.get("cwd"))
    ti = data.get("tool_input") or {}
    f = rel(root, ti.get("file_path") or ti.get("notebook_path"))
    if f:
        log(db, sid, "edit", file=f)


def on_end(db, root, sid, data) -> None:
    touch(db, sid, data.get("cwd"))
    db.execute("UPDATE sessions SET ended=? WHERE id=?", (time.time(), sid))
    log(db, sid, "end")


HOOKS = {
    "session-start": on_session_start,
    "prompt": on_prompt,
    "pre-edit": on_pre_edit,
    "post-edit": on_post_edit,
    "session-end": on_end,
}


def run_hook(name: str) -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    sid = data.get("session_id")
    handler = HOOKS.get(name)
    if not sid or not handler:
        return 0
    root = project_root(data.get("cwd"))
    try:
        db = connect(root)
        with db:
            handler(db, root, sid, data)
    except Exception as e:  # never break the user's session over coordination
        print(f"crosstalk: {e}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- CLI

def cli_db():
    root = project_root(None)
    return root, connect(root)


def cmd_status() -> int:
    root, db = cli_db()
    me = os.environ.get("CROSSTALK_SESSION", "")
    live = db.execute(
        "SELECT * FROM sessions WHERE ended IS NULL AND last_seen>? ORDER BY last_seen DESC",
        (time.time() - LIVE_SECONDS,),
    ).fetchall()
    print(f"crosstalk · {root}")
    print(f"{len(live)} live session(s)")
    for s in live:
        tag = "  (you)" if s["id"] == me else ""
        print(describe(db, s) + tag)
    notes = recent_notes(db)
    if notes:
        print("\nShared notes:")
        labels = labels_map(db)
        for n in notes:
            print(fmt_event(n, labels))
    return 0


def cmd_note(text: str) -> int:
    text = " ".join(text.split())
    if not text:
        print("usage: crosstalk.py note <text>", file=sys.stderr)
        return 1
    root, db = cli_db()
    sid = os.environ.get("CROSSTALK_SESSION") or f"cli-{os.getpid()}"
    with db:
        if not os.environ.get("CROSSTALK_SESSION"):
            db.execute(
                "INSERT OR IGNORE INTO sessions(id,label,cwd,started,last_seen,ended) VALUES(?,?,?,?,?,?)",
                (sid, "human", os.getcwd(), time.time(), time.time(), time.time()),
            )
        else:
            touch(db, sid, os.getcwd())
        log(db, sid, "note", text=text[:500])
    print(f"crosstalk: shared with the other sessions → {text}")
    return 0


def cmd_log(n: int) -> int:
    root, db = cli_db()
    labels = labels_map(db)
    rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()[::-1]
    for e in rows:
        print(fmt_event(e, labels))
    if not rows:
        print("crosstalk: nothing yet")
    return 0


def cmd_forget() -> int:
    root, db = cli_db()
    with db:
        db.execute("DELETE FROM events")
        db.execute("DELETE FROM sessions")
    print(f"crosstalk: cleared the shared brain for {root}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "hook" and rest:
        return run_hook(rest[0])
    if cmd == "status":
        return cmd_status()
    if cmd == "note":
        return cmd_note(" ".join(rest))
    if cmd == "log":
        return cmd_log(int(rest[0]) if rest else 30)
    if cmd == "forget":
        return cmd_forget()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
