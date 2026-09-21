"""SQLite 持久化与固定夹具播种。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
DEFAULT_DB = ROOT / "carbon_branches.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS curves (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    knot_hash TEXT NOT NULL,
    knots_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    age REAL NOT NULL,
    sd REAL NOT NULL,
    material TEXT NOT NULL,
    reservoir REAL NOT NULL DEFAULT 0,
    layer INTEGER,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY,
    sample_code TEXT NOT NULL,
    curve_version TEXT NOT NULL,
    direction TEXT NOT NULL,
    prior_kind TEXT NOT NULL,
    group_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (curve_version) REFERENCES curves(version)
);
CREATE TABLE IF NOT EXISTS results (
    branch_id INTEGER PRIMARY KEY,
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    FOREIGN KEY (branch_id) REFERENCES branches(id)
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    branch_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def db_path() -> Path:
    return Path(os.environ.get("CARBON_DB", str(DEFAULT_DB)))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | Path | None = None) -> Path:
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) FROM curves").fetchone()[0]
        if count == 0:
            seed_curves(conn)
    return path


def seed_curves(conn: sqlite3.Connection) -> None:
    payload = json.loads((FIXTURES / "curves.json").read_text(encoding="utf-8"))
    from .calibration import Curve

    for item in payload["curves"]:
        curve = Curve(
            version=item["version"],
            name=item["name"],
            summary=item["summary"],
            source=item["source"],
            knots=tuple((float(a), float(b)) for a, b in item["knots"]),
            created_at=item["created_at"],
        )
        conn.execute(
            "INSERT INTO curves(version,name,summary,source,created_at,knot_hash,knots_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                curve.version,
                curve.name,
                curve.summary,
                curve.source,
                curve.created_at,
                curve.knot_hash,
                json.dumps([[a, b] for a, b in curve.knots]),
            ),
        )


def get_curve(conn: sqlite3.Connection, version: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM curves WHERE version=?", (version,)).fetchone()
    if row is None:
        raise LookupError(version)
    return dict(row)


def list_curves(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT version,name,summary,source,created_at,knot_hash,"
        "json_array_length(knots_json) AS knot_count FROM curves ORDER BY version"
    ).fetchall()
    return [dict(r) for r in rows]


def log_action(
    conn: sqlite3.Connection,
    action: str,
    detail: str,
    branch_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO run_log(action,detail,branch_id) VALUES (?,?,?)",
        (action, detail, branch_id),
    )


def clear_workspace(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM results")
    conn.execute("DELETE FROM branches")
    conn.execute("DELETE FROM samples")
    conn.execute("DELETE FROM run_log")
