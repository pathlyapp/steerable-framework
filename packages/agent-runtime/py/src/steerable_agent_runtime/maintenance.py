"""Offline maintenance for SqliteStorage databases (W2.6.2).

Four jobs, runnable as a CLI (``python -m steerable_agent_runtime.maintenance``)
or imported as functions:

- ``check``    — ``PRAGMA integrity_check``; the pre-flight for every other
                 job and the first diagnostic when a database is suspect.
- ``compact``  — delete traces (with their spans/events) older than a cutoff,
                 then VACUUM. Sessions, messages and history records are
                 never touched: the history record is append-only and
                 auditable (W2.6.3), compaction of *content* belongs to the
                 loop's declared CompactionBoundary, not to offline tooling.
- ``archive``  — copy sessions (plus their messages) updated before a cutoff
                 into a separate archive database, then delete them from the
                 main one. Archiving is a move: the archive file is a full
                 SqliteStorage database and can be opened directly.
- ``salvage``  — best-effort export of every decodable row to JSONL when the
                 database is too corrupt for normal access. Rows that fail to
                 decode are skipped and counted, never silently dropped from
                 the report.

All jobs open the database themselves and refuse to run when the integrity
check fails (except ``salvage``, which exists for exactly that case).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_TABLES = ("sessions", "agents", "messages", "traces", "spans", "events", "history")


@dataclass(slots=True)
class MaintenanceReport:
    """Outcome of one maintenance job. ``details`` carries per-step counts."""

    job: str
    ok: bool
    details: dict[str, int | str] = field(default_factory=dict)


def _connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _cutoff_iso(older_than_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()


def check(path: str) -> MaintenanceReport:
    """Integrity pre-flight. Every other job refuses to run on failure."""
    db = _connect(path)
    try:
        rows = db.execute("PRAGMA integrity_check").fetchall()
        problems = [r[0] for r in rows if r[0] != "ok"]
        return MaintenanceReport(
            job="check",
            ok=not problems,
            details={"problems": "; ".join(problems)} if problems else {},
        )
    finally:
        db.close()


def compact(path: str, *, older_than_days: int) -> MaintenanceReport:
    """Drop traces/spans/events older than the cutoff, then VACUUM."""
    pre = check(path)
    if not pre.ok:
        return MaintenanceReport("compact", False, {"refused": str(pre.details)})
    cutoff = _cutoff_iso(older_than_days)
    db = _connect(path)
    try:
        old = [
            r[0]
            for r in db.execute(
                "SELECT trace_id FROM traces WHERE created_at < ?", (cutoff,)
            ).fetchall()
        ]
        with db:
            for trace_id in old:
                db.execute("DELETE FROM spans WHERE trace_id = ?", (trace_id,))
                db.execute("DELETE FROM events WHERE trace_id = ?", (trace_id,))
            db.execute("DELETE FROM traces WHERE created_at < ?", (cutoff,))
        db.execute("VACUUM")
        return MaintenanceReport(
            "compact", True, {"traces_removed": len(old), "cutoff": cutoff}
        )
    finally:
        db.close()


def archive(path: str, archive_path: str, *, older_than_days: int) -> MaintenanceReport:
    """Move sessions (with their messages) older than the cutoff to an
    archive database. History records stay — they are the auditable record
    and may still be referenced by active branches."""
    pre = check(path)
    if not pre.ok:
        return MaintenanceReport("archive", False, {"refused": str(pre.details)})
    cutoff = _cutoff_iso(older_than_days)

    # The archive is a real SqliteStorage database: import here so the
    # schema DDL lives in exactly one place.
    from .storage.sqlite_store import _SCHEMA

    main = _connect(path)
    try:
        sessions = main.execute(
            "SELECT * FROM sessions WHERE updated_at < ?", (cutoff,)
        ).fetchall()
        if not sessions:
            return MaintenanceReport("archive", True, {"sessions_archived": 0})
        chat_ids = [s["chat_id"] for s in sessions]
        session_ids = [s["session_id"] for s in sessions]

        arch = _connect(archive_path)
        try:
            arch.executescript(_SCHEMA)
            with arch:
                arch.executemany(
                    "INSERT OR REPLACE INTO sessions"
                    " (session_id, user_id, chat_id, is_active, updated_at, data)"
                    " VALUES (:session_id, :user_id, :chat_id, :is_active,"
                    "  :updated_at, :data)",
                    [dict(s) for s in sessions],
                )
                marks = ",".join("?" for _ in chat_ids)
                messages = main.execute(
                    f"SELECT * FROM messages WHERE chat_id IN ({marks})", chat_ids
                ).fetchall()
                arch.executemany(
                    "INSERT OR REPLACE INTO messages (id, chat_id, created_at, data)"
                    " VALUES (:id, :chat_id, :created_at, :data)",
                    [dict(m) for m in messages],
                )
        finally:
            arch.close()

        with main:
            s_marks = ",".join("?" for _ in session_ids)
            c_marks = ",".join("?" for _ in chat_ids)
            main.execute(
                f"DELETE FROM sessions WHERE session_id IN ({s_marks})", session_ids
            )
            main.execute(f"DELETE FROM messages WHERE chat_id IN ({c_marks})", chat_ids)
        main.execute("VACUUM")
        return MaintenanceReport(
            "archive",
            True,
            {"sessions_archived": len(sessions), "messages_archived": len(messages)},
        )
    finally:
        main.close()


def salvage(path: str, out_path: str) -> MaintenanceReport:
    """Dump every decodable row to JSONL. Skips (and counts) rows that fail
    to read — the report is the accounting, nothing vanishes silently."""
    db = _connect(path)
    written = 0
    skipped = 0
    try:
        with open(out_path, "w", encoding="utf-8") as out:
            for table in _TABLES:
                try:
                    rows = db.execute(f"SELECT * FROM {table}").fetchall()
                except sqlite3.DatabaseError:
                    skipped += 1
                    continue
                for row in rows:
                    try:
                        out.write(
                            json.dumps(
                                {"table": table, "row": dict(row)},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        written += 1
                    except (TypeError, ValueError, sqlite3.DatabaseError):
                        skipped += 1
        return MaintenanceReport(
            "salvage", True, {"rows_written": written, "rows_skipped": skipped}
        )
    finally:
        db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steerable-maintenance",
        description="Offline maintenance for steerable SqliteStorage databases.",
    )
    parser.add_argument("database", help="Path to the sqlite database file.")
    sub = parser.add_subparsers(dest="job", required=True)
    sub.add_parser("check", help="PRAGMA integrity_check.")
    compact_p = sub.add_parser("compact", help="Drop old traces and VACUUM.")
    compact_p.add_argument(
        "--older-than-days", type=int, required=True, help="Cutoff age in days."
    )
    archive_p = sub.add_parser(
        "archive", help="Move old sessions+messages to an archive database."
    )
    archive_p.add_argument(
        "--older-than-days", type=int, required=True, help="Cutoff age in days."
    )
    archive_p.add_argument(
        "archive_db", help="Archive database path (created if missing)."
    )
    salvage_p = sub.add_parser("salvage", help="Best-effort JSONL export.")
    salvage_p.add_argument("out", help="Output JSONL path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.job == "check":
        report = check(args.database)
    elif args.job == "compact":
        report = compact(args.database, older_than_days=args.older_than_days)
    elif args.job == "archive":
        report = archive(
            args.database, args.archive_db, older_than_days=args.older_than_days
        )
    else:
        report = salvage(args.database, args.out)
    print(json.dumps({"job": report.job, "ok": report.ok, **report.details}))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
