#!/usr/bin/env python3
"""
task_queue.py — SQLite-backed async task queue for Nico Corp
=============================================================
Inspired by Boris Cherny's fleet workflow: queue work, let it cook during
safe windows, surface results automatically.

Queue DB: tools/data/task_queue.db

Task types:
  research  — fetch a URL, return first 2000 chars
  subagent  — marks failed (requires main session to spawn)
  reminder  — immediate: result = payload["message"]
  cron      — marks done (handled by openclaw scheduler)

CLI:
  python3 tools/task_queue.py --list
  python3 tools/task_queue.py --pending
  python3 tools/task_queue.py --add --title "Research X" --type research --payload '{"url":"https://..."}'
  python3 tools/task_queue.py --cancel <id>
  python3 tools/task_queue.py --status <id>
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path

# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------

WORKSPACE = Path(__file__).parent.parent
DB_PATH   = WORKSPACE / "tools" / "data" / "task_queue.db"

DEFAULT_TELEGRAM_CHAT_ID = "5463998499"

# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    title             TEXT NOT NULL,
    task_type         TEXT NOT NULL CHECK(task_type IN ('research','subagent','reminder','cron')),
    payload           TEXT NOT NULL DEFAULT '{}',
    priority          INTEGER NOT NULL DEFAULT 5,
    status            TEXT NOT NULL DEFAULT 'pending'
                          CHECK(status IN ('pending','running','done','failed','cancelled')),
    scheduled_after   TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    result_summary    TEXT,
    telegram_chat_id  TEXT NOT NULL DEFAULT '5463998499'
);
"""


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_task(
    title: str,
    task_type: str,
    payload: dict | None = None,
    priority: int = 5,
    scheduled_after: str | None = None,
    telegram_chat_id: str = DEFAULT_TELEGRAM_CHAT_ID,
) -> int:
    """Insert a new task. Returns the new task id."""
    if task_type not in ("research", "subagent", "reminder", "cron"):
        raise ValueError(f"Invalid task_type: {task_type}")
    payload_json = json.dumps(payload or {})
    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (created_at, title, task_type, payload, priority,
                               scheduled_after, telegram_chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_now(), title, task_type, payload_json, priority, scheduled_after, telegram_chat_id),
        )
        task_id = cur.lastrowid
    print(f"[task_queue] Added task #{task_id}: {title!r} ({task_type}, priority={priority})")
    return task_id


def get_pending_tasks(limit: int = 10) -> list[dict]:
    """Return pending tasks ordered by priority (1=urgent first), then created_at."""
    now = _now()
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'pending'
              AND (scheduled_after IS NULL OR scheduled_after <= ?)
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_task(task_id: int) -> dict | None:
    """Fetch a single task by id. Returns None if not found."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def mark_running(task_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status='running', started_at=? WHERE id=?",
            (_now(), task_id),
        )


def mark_done(task_id: int, result_summary: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, result_summary=? WHERE id=?",
            (_now(), result_summary, task_id),
        )


def mark_failed(task_id: int, reason: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', completed_at=?, result_summary=? WHERE id=?",
            (_now(), reason, task_id),
        )


def cancel_task(task_id: int) -> bool:
    """Cancel a pending/running task. Returns True if cancelled."""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='cancelled' WHERE id=? AND status IN ('pending','running')",
            (task_id,),
        )
        changed = cur.rowcount > 0
    if changed:
        print(f"[task_queue] Task #{task_id} cancelled.")
    else:
        print(f"[task_queue] Task #{task_id} not found or already terminal.")
    return changed


def list_tasks(status: str | None = None, limit: int = 50) -> list[dict]:
    """List tasks, optionally filtered by status."""
    with _get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_tasks(tasks: list[dict]) -> None:
    if not tasks:
        print("  (none)")
        return
    for t in tasks:
        scheduled = f" [after {t['scheduled_after']}]" if t.get("scheduled_after") else ""
        result_snippet = ""
        if t.get("result_summary"):
            result_snippet = " → " + t["result_summary"][:60].replace("\n", " ")
        print(
            f"  #{t['id']:>4} [{t['status']:<10}] p={t['priority']} "
            f"{t['task_type']:<10} {t['title']!r}{scheduled}{result_snippet}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Nico Corp async task queue")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list",    action="store_true", help="List all tasks (latest 50)")
    group.add_argument("--pending", action="store_true", help="List pending tasks")
    group.add_argument("--add",     action="store_true", help="Add a new task")
    group.add_argument("--cancel",  type=int,            metavar="ID", help="Cancel a task by id")
    group.add_argument("--status",  type=int,            metavar="ID", help="Show a task by id")

    # --add options
    parser.add_argument("--title",           type=str, help="Task title")
    parser.add_argument("--type",            type=str, dest="task_type",
                        choices=["research","subagent","reminder","cron"],
                        help="Task type")
    parser.add_argument("--payload",         type=str, default="{}", help="JSON payload string")
    parser.add_argument("--priority",        type=int, default=5,   help="Priority 1-10 (1=urgent)")
    parser.add_argument("--scheduled-after", type=str, default=None,
                        metavar="ISO8601",   help="Don't run before this UTC timestamp")
    parser.add_argument("--chat-id",         type=str, default=DEFAULT_TELEGRAM_CHAT_ID,
                        help="Telegram chat id for result delivery")

    args = parser.parse_args()

    if args.list:
        tasks = list_tasks()
        print(f"All tasks ({len(tasks)}):")
        _print_tasks(tasks)

    elif args.pending:
        tasks = get_pending_tasks()
        print(f"Pending tasks ({len(tasks)}):")
        _print_tasks(tasks)

    elif args.cancel:
        cancel_task(args.cancel)

    elif args.status:
        task = get_task(args.status)
        if task:
            print(json.dumps(task, indent=2))
        else:
            print(f"Task #{args.status} not found.")
            sys.exit(1)

    elif args.add:
        if not args.title:
            parser.error("--add requires --title")
        if not args.task_type:
            parser.error("--add requires --type")
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            parser.error(f"--payload is not valid JSON: {e}")
        task_id = add_task(
            title=args.title,
            task_type=args.task_type,
            payload=payload,
            priority=args.priority,
            scheduled_after=args.scheduled_after,
            telegram_chat_id=args.chat_id,
        )
        print(f"Created task #{task_id}")


if __name__ == "__main__":
    main()
