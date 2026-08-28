"""Durable, generic per-owner checkpoint store, SQLite-backed.

Every `save_checkpoint()` call is a synchronous, durable write to a local
`.db` file — no network dependency, so "resume from any state" (see
checkpoint.py) never silently means "resume from any state unless some
other service happens to be reachable."

Keyed by `(owner_kind, owner_id)` — e.g. `owner_kind="forge_run"`,
`owner_id=<run_id>` for `checkpoint.RunCheckpointer`. Pick your own
`owner_kind` for anything else you want durably checkpointed so IDs from
different owners can never collide in this shared table. Each
`save_checkpoint()` call INSERTs a new row rather than overwriting one in
place, so the full history for an owner is always available
(`load_checkpoints_for`), while `load_checkpoint` returns just the latest.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: Overridable via CHECKPOINT_DB_PATH — e.g. tests point this at a
#: tmp_path file so runs don't share state across test cases.
DEFAULT_DB_PATH = Path(os.environ.get("CHECKPOINT_DB_PATH", "./data/checkpoints.db"))

#: One sqlite3.Connection per (thread, db_path) — sqlite3 connections
#: aren't safe to share across threads, and this store is called both from
#: a main thread and worker threads (a repair run driven via
#: asyncio.to_thread, or a plain ThreadPoolExecutor), so a single
#: process-wide connection would be a real concurrency hazard.
_local = threading.local()


def _now_iso() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _connect(db_path: Path) -> sqlite3.Connection:
    cache: dict = getattr(_local, "connections", None)
    if cache is None:
        cache = {}
        _local.connections = cache
    key = str(db_path)
    conn = cache.get(key)
    if conn is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS checkpoints ("
            "  seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  owner_kind TEXT NOT NULL,"
            "  owner_id TEXT NOT NULL,"
            "  data TEXT NOT NULL,"
            "  created_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_owner "
            "ON checkpoints(owner_kind, owner_id, seq)"
        )
        conn.commit()
        cache[key] = conn
    return conn


@dataclass
class CheckpointRecord:
    owner_kind: str
    owner_id: str
    data: dict[str, Any]
    created_at: str = ""
    #: SQLite row id — None for a record that hasn't been saved yet.
    seq: Optional[int] = None


def save_checkpoint(
    owner_kind: str, owner_id: str, data: dict[str, Any], *, db_path: Optional[Path] = None,
) -> CheckpointRecord:
    db_path = db_path or DEFAULT_DB_PATH
    conn = _connect(db_path)
    created_at = _now_iso()
    payload = json.dumps(data)
    cur = conn.execute(
        "INSERT INTO checkpoints (owner_kind, owner_id, data, created_at) VALUES (?, ?, ?, ?)",
        (owner_kind, owner_id, payload, created_at),
    )
    conn.commit()
    return CheckpointRecord(owner_kind=owner_kind, owner_id=owner_id, data=data,
                            created_at=created_at, seq=cur.lastrowid)


def load_checkpoint(
    owner_kind: str, owner_id: str, *, db_path: Optional[Path] = None,
) -> Optional[CheckpointRecord]:
    """Latest checkpoint for `(owner_kind, owner_id)`, or None if there is none."""
    db_path = db_path or DEFAULT_DB_PATH
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT seq, data, created_at FROM checkpoints "
        "WHERE owner_kind = ? AND owner_id = ? ORDER BY seq DESC LIMIT 1",
        (owner_kind, owner_id),
    ).fetchone()
    if row is None:
        return None
    seq, data_json, created_at = row
    return CheckpointRecord(owner_kind=owner_kind, owner_id=owner_id,
                            data=json.loads(data_json), created_at=created_at, seq=seq)


def load_checkpoints_for(
    owner_kind: str, owner_id: str, *, db_path: Optional[Path] = None,
) -> list[CheckpointRecord]:
    """Full history for `(owner_kind, owner_id)`, oldest first."""
    db_path = db_path or DEFAULT_DB_PATH
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT seq, data, created_at FROM checkpoints "
        "WHERE owner_kind = ? AND owner_id = ? ORDER BY seq ASC",
        (owner_kind, owner_id),
    ).fetchall()
    return [
        CheckpointRecord(owner_kind=owner_kind, owner_id=owner_id,
                         data=json.loads(data_json), created_at=created_at, seq=seq)
        for seq, data_json, created_at in rows
    ]


def list_latest_per_owner(owner_kind: str, *, db_path: Optional[Path] = None) -> list[CheckpointRecord]:
    """Latest checkpoint row for every distinct owner_id under `owner_kind`."""
    db_path = db_path or DEFAULT_DB_PATH
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT c.seq, c.owner_id, c.data, c.created_at FROM checkpoints c "
        "INNER JOIN ("
        "  SELECT owner_id, MAX(seq) AS max_seq FROM checkpoints "
        "  WHERE owner_kind = ? GROUP BY owner_id"
        ") latest ON c.owner_id = latest.owner_id AND c.seq = latest.max_seq "
        "WHERE c.owner_kind = ? ORDER BY c.seq DESC",
        (owner_kind, owner_kind),
    ).fetchall()
    return [
        CheckpointRecord(owner_kind=owner_kind, owner_id=owner_id,
                         data=json.loads(data_json), created_at=created_at, seq=seq)
        for seq, owner_id, data_json, created_at in rows
    ]
