"""SQLite persistence, inventory ledger and transactional helpers.

Inventory is derived from an append-only ledger:

* a receipt is a positive entry;
* dosing a run writes the negative usage entry in the *same* transaction that
  flips the run from ``planned`` to ``dosed``;
* cancelling is only allowed for a ``planned`` run and writes no ledger entry -
  its hold simply disappears.

``available = ledger_balance - sum(usage of planned runs)``.  Therefore dosing
does not change availability (it converts a hold into consumed material), and
only cancelling an un-started run releases material.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "formulab.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    component_code TEXT NOT NULL,
    delta REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('receipt', 'dose', 'adjustment')),
    run_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    amounts_json TEXT NOT NULL,
    level TEXT,
    response REAL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    design_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    design_json TEXT NOT NULL,
    requested_size INTEGER NOT NULL,
    feasible_size INTEGER NOT NULL,
    partial INTEGER NOT NULL,
    blocking_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    search TEXT NOT NULL,
    UNIQUE (task_id, revision)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    amounts_json TEXT NOT NULL,
    level TEXT,
    usage_json TEXT NOT NULL,
    charge_mass REAL NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    duplicate INTEGER NOT NULL DEFAULT 0,
    mixture_info REAL NOT NULL DEFAULT 0,
    total_info REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'dosed', 'completed', 'failed', 'cancelled')),
    created_at TEXT NOT NULL,
    dosed_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    value REAL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_study ON stock_entries(study_id, component_code);
CREATE INDEX IF NOT EXISTS idx_runs_study_status ON runs(study_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, status);
CREATE INDEX IF NOT EXISTS idx_history_study ON history(study_id);
"""

_write_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


RUNS_COLUMNS = (
    "id, study_id, task_id, batch_id, ordinal, amounts_json, level, usage_json, "
    "charge_mass, locked, duplicate, mixture_info, total_info, status, created_at, "
    "dosed_at, completed_at, cancelled_at"
)


def _migrate_runs_failed_status(conn: sqlite3.Connection) -> None:
    """Allow the ``failed`` run status on databases created before it existed."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone()
    if row is None or "'failed'" in (row["sql"] or ""):
        return
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        ALTER TABLE runs RENAME TO runs_old;
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            study_id INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            amounts_json TEXT NOT NULL,
            level TEXT,
            usage_json TEXT NOT NULL,
            charge_mass REAL NOT NULL,
            locked INTEGER NOT NULL DEFAULT 0,
            duplicate INTEGER NOT NULL DEFAULT 0,
            mixture_info REAL NOT NULL DEFAULT 0,
            total_info REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned','dosed','completed','failed','cancelled')),
            created_at TEXT NOT NULL,
            dosed_at TEXT,
            completed_at TEXT,
            cancelled_at TEXT
        );
        INSERT INTO runs (id, study_id, task_id, batch_id, ordinal, amounts_json, level,
                          usage_json, charge_mass, locked, duplicate, mixture_info,
                          total_info, status, created_at, dosed_at, completed_at,
                          cancelled_at)
        SELECT id, study_id, task_id, batch_id, ordinal, amounts_json, level,
               usage_json, charge_mass, locked, duplicate, mixture_info, total_info,
               status, created_at, dosed_at, completed_at, cancelled_at
        FROM runs_old;
        DROP TABLE runs_old;
        CREATE INDEX IF NOT EXISTS idx_runs_study_status ON runs(study_id, status);
        CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, status);
        PRAGMA foreign_keys = ON;
        """
    )


def init_db(path: Path | str | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate_runs_failed_status(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Serialisable write transaction.

    ``BEGIN IMMEDIATE`` takes the database write lock before any read, so two
    concurrent planners cannot both observe the same free stock.  The process
    lock covers the very short window before the transaction starts.
    """
    conn = connect(path)
    try:
        with _write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


@contextmanager
def read_conn(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Study helpers


def get_study(conn: sqlite3.Connection, study_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    if row is None:
        raise KeyError("study")
    return row


def load_config(conn: sqlite3.Connection, study_id: int) -> dict:
    row = get_study(conn, study_id)
    return json.loads(row["config_json"])


def save_config(conn: sqlite3.Connection, study_id: int, config: dict) -> None:
    conn.execute(
        "UPDATE studies SET config_json = ? WHERE id = ?",
        (json.dumps(config, ensure_ascii=False), study_id),
    )


def component_codes(config: dict) -> list[str]:
    return [c["code"] for c in config["components"]]


def ledger_balance(conn: sqlite3.Connection, study_id: int) -> dict[str, float]:
    rows = conn.execute(
        "SELECT component_code, SUM(delta) AS bal FROM stock_entries "
        "WHERE study_id = ? GROUP BY component_code",
        (study_id,),
    ).fetchall()
    return {r["component_code"]: float(r["bal"] or 0.0) for r in rows}


def held_amounts(conn: sqlite3.Connection, study_id: int, exclude_task: int | None = None) -> dict[str, float]:
    rows = conn.execute(
        "SELECT amounts_json, usage_json FROM runs WHERE study_id = ? AND status = 'planned'"
        + (" AND task_id != ?" if exclude_task is not None else ""),
        (study_id, exclude_task) if exclude_task is not None else (study_id,),
    ).fetchall()
    held: dict[str, float] = {}
    for r in rows:
        for code, qty in json.loads(r["usage_json"]).items():
            held[code] = held.get(code, 0.0) + float(qty)
    return held


def inventory(conn: sqlite3.Connection, study_id: int, config: dict) -> list[dict]:
    balance = ledger_balance(conn, study_id)
    held = held_amounts(conn, study_id)
    out = []
    for comp in config["components"]:
        code = comp["code"]
        received = float(balance.get(code, 0.0))
        holds = float(held.get(code, 0.0))
        out.append(
            {
                "code": code,
                "name": comp.get("name", code),
                "unit": comp.get("unit", "g"),
                "balance": round(received, 9),
                "held": round(holds, 9),
                "available": round(received - holds, 9),
            }
        )
    return out


def available_vector(conn: sqlite3.Connection, study_id: int, config: dict) -> list[float]:
    inv = {row["code"]: row["available"] for row in inventory(conn, study_id, config)}
    return [max(0.0, inv[c["code"]]) for c in config["components"]]


def available_for_task(
    conn: sqlite3.Connection, study_id: int, config: dict, task_id: int
) -> list[float]:
    """Material this task may commit: ledger balance minus *other* tasks' holds.

    A task's own previous revision is about to be cancelled, so its holds are
    returned to the pool for the new revision - but only for this task.  Other
    tasks keep their holds, which is what prevents two planners from spending
    the same material.
    """
    balance = ledger_balance(conn, study_id)
    held_elsewhere = held_amounts(conn, study_id, exclude_task=task_id)
    return [
        max(0.0, float(balance.get(c["code"], 0.0)) - float(held_elsewhere.get(c["code"], 0.0)))
        for c in config["components"]
    ]


def response_rows(conn: sqlite3.Connection, study_id: int) -> list[dict]:
    """All completed observations: imported history plus dosed runs with responses."""
    out: list[dict] = []
    for row in conn.execute(
        "SELECT * FROM history WHERE study_id = ? AND response IS NOT NULL ORDER BY id",
        (study_id,),
    ):
        out.append(
            {
                "source": "history",
                "ref": row["label"],
                "amounts": json.loads(row["amounts_json"]),
                "level": row["level"],
                "response": float(row["response"]),
                "note": row["note"],
            }
        )
    for row in conn.execute(
        "SELECT r.*, resp.value AS value, resp.note AS note FROM runs r "
        "JOIN responses resp ON resp.run_id = r.id "
        "WHERE r.study_id = ? AND resp.value IS NOT NULL ORDER BY r.id",
        (study_id,),
    ):
        out.append(
            {
                "source": "run",
                "ref": f"R{row['id']}",
                "run_id": row["id"],
                "amounts": json.loads(row["amounts_json"]),
                "level": row["level"],
                "response": float(row["value"]),
                "note": row["note"],
                "mixture_info": row["mixture_info"],
                "total_info": row["total_info"],
            }
        )
    return out


def failed_runs(conn: sqlite3.Connection, study_id: int) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT * FROM history WHERE study_id = ? AND response IS NULL ORDER BY id",
        (study_id,),
    ):
        out.append(
            {
                "source": "history",
                "ref": row["label"],
                "amounts": json.loads(row["amounts_json"]),
                "level": row["level"],
                "note": row["note"],
                "status": "failed",
            }
        )
    for row in conn.execute(
        "SELECT r.* FROM runs r WHERE r.study_id = ? "
        "AND (r.status = 'failed' OR (r.status = 'dosed' AND NOT EXISTS "
        "     (SELECT 1 FROM responses resp WHERE resp.run_id = r.id AND resp.value IS NOT NULL))) "
        "ORDER BY r.id",
        (study_id,),
    ):
        out.append(
            {
                "source": "run",
                "ref": f"R{row['id']}",
                "run_id": row["id"],
                "amounts": json.loads(row["amounts_json"]),
                "level": row["level"],
                "note": "标记为失败：已消耗原料，但无有效响应，不参与拟合"
                if row["status"] == "failed"
                else "已投料，等待响应",
                "status": "failed" if row["status"] == "failed" else "dosed_no_response",
            }
        )
    return out
