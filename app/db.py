from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .calibration import GRID_START, GRID_STEP, GRID_STOP, canonical_material, read_curve_csv

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "carbon_branch.db"
CURVE_DIR = ROOT / "fixtures" / "curves"

CURVE_FILES = {
    "LOCAL-13k-v1": {
        "name": "本地碳十四校准曲线",
        "version": "LOCAL-13k-v1",
        "summary": "本地版本化夹具：含约 4855-5505 cal BP 的平台/波动段，5000 BP 似然映射出三个分离峰。",
        "path": CURVE_DIR / "LOCAL-13k-v1.csv",
    },
    "LOCAL-13k-v2": {
        "name": "本地碳十四校准曲线",
        "version": "LOCAL-13k-v2",
        "summary": "v1 的本地修订版：平台高度和谷底不同；结果独立保存，禁止与 v1 合并。",
        "path": CURVE_DIR / "LOCAL-13k-v2.csv",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(os.environ.get("CARBON_DB", str(DB_PATH)))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(seed: bool = True) -> None:
    Path(os.environ.get("CARBON_DB", str(DB_PATH))).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS curves (
                curve_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                version TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL,
                support_start_bp INTEGER NOT NULL,
                support_stop_bp INTEGER NOT NULL,
                grid_step_years INTEGER NOT NULL,
                knot_count INTEGER NOT NULL,
                fingerprint_sha256 TEXT NOT NULL,
                points_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS samples (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                age_bp REAL NOT NULL,
                sigma_bp REAL NOT NULL,
                material TEXT NOT NULL,
                reservoir_correction REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS branches (
                branch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id INTEGER NOT NULL REFERENCES samples(sample_id) ON DELETE CASCADE,
                curve_id TEXT NOT NULL REFERENCES curves(curve_id),
                prior_key TEXT NOT NULL,
                direction TEXT NOT NULL,
                coverages_json TEXT NOT NULL,
                fingerprint_sha256 TEXT NOT NULL,
                effective_target_bp REAL NOT NULL,
                normalization REAL NOT NULL,
                prior_profile_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(sample_id, curve_id, prior_key, direction, coverages_json)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity TEXT NOT NULL,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
    if seed:
        seed_curves()


def seed_curves() -> dict[str, Any]:
    inserted: list[str] = []
    reused: list[str] = []
    with connect() as conn:
        for curve_id, metadata in CURVE_FILES.items():
            points, fingerprint = read_curve_csv(metadata["path"])
            existing = conn.execute("SELECT fingerprint_sha256 FROM curves WHERE curve_id = ?", (curve_id,)).fetchone()
            if existing:
                if existing["fingerprint_sha256"] != fingerprint:
                    raise RuntimeError(f"曲线 {curve_id} 已入库且指纹变化；旧分支仍绑定旧节点，不允许覆盖")
                reused.append(curve_id)
                continue
            conn.execute(
                """
                INSERT INTO curves
                (curve_id,name,version,summary,support_start_bp,support_stop_bp,grid_step_years,
                 knot_count,fingerprint_sha256,points_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    curve_id,
                    metadata["name"],
                    metadata["version"],
                    metadata["summary"],
                    GRID_START,
                    GRID_STOP,
                    GRID_STEP,
                    len(points),
                    fingerprint,
                    json.dumps(points, separators=(",", ":")),
                    utc_now(),
                ),
            )
            inserted.append(curve_id)
    return {"inserted": inserted, "reused": reused}


def record_run(action: str, entity: str, request: Any, status: str, detail: Any = "") -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO runs(action,entity,request_json,status,detail,created_at) VALUES (?,?,?,?,?,?)",
            (action, entity, json.dumps(request, ensure_ascii=False, sort_keys=True), status, json.dumps(detail, ensure_ascii=False), utc_now()),
        )
        return int(cursor.lastrowid)


def list_curves() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT curve_id,name,version,summary,support_start_bp,support_stop_bp,
                   grid_step_years,knot_count,fingerprint_sha256,created_at
            FROM curves ORDER BY version
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_curve(conn: sqlite3.Connection, curve_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM curves WHERE curve_id = ?", (curve_id,)).fetchone()
    if row is None:
        raise KeyError("未知曲线版本")
    return row


def parse_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text.strip())))


def _first_value(row: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        if alias in row and str(row[alias]).strip() != "":
            return str(row[alias]).strip()
    return None


def normalize_sample_row(row: dict[str, Any]) -> dict[str, Any]:
    code = _first_value(row, ("code", "id", "sample", "sample_code", "编号", "代码", "实验室编号"))
    age = _first_value(row, ("age_bp", "age", "c14_age", "experimental_age", "年龄", "实验年龄"))
    sigma = _first_value(row, ("sigma_bp", "sigma", "std", "sd", "error", "标准差", "误差"))
    material = _first_value(row, ("material", "material_type", "材料", "材料类型")) or "unassigned"
    reservoir = _first_value(row, ("reservoir_correction", "reservoir", "delta_r", "library_correction", "库效应校正", "库效应")) or "0"
    if not code or age is None or sigma is None:
        raise ValueError("每行至少需要 code、age_bp、sigma_bp")
    age_value = float(age)
    sigma_value = float(sigma)
    reservoir_value = float(reservoir)
    if sigma_value <= 0:
        raise ValueError(f"{code}: 标准差必须为正数")
    return {
        "code": code,
        "age_bp": age_value,
        "sigma_bp": sigma_value,
        "material": canonical_material(material),
        "reservoir_correction": reservoir_value,
        "raw": row,
    }


def import_rows(rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
    if not rows:
        raise ValueError("没有可导入的数据行")
    normalized = [normalize_sample_row(row) for row in rows]
    inserted, replaced = [], []
    with connect() as conn:
        for item in normalized:
            existing = conn.execute("SELECT sample_id FROM samples WHERE code = ?", (item["code"],)).fetchone()
            payload = (
                item["code"],
                item["age_bp"],
                item["sigma_bp"],
                item["material"],
                item["reservoir_correction"],
                source,
                json.dumps(item["raw"], ensure_ascii=False, sort_keys=True),
                utc_now(),
            )
            if existing:
                conn.execute(
                    """
                    UPDATE samples SET age_bp=?, sigma_bp=?, material=?, reservoir_correction=?,
                    source=?, raw_json=?, imported_at=? WHERE code=?
                    """,
                    payload[1:] + (item["code"],),
                )
                replaced.append(item["code"])
            else:
                conn.execute(
                    """
                    INSERT INTO samples
                    (code,age_bp,sigma_bp,material,reservoir_correction,source,raw_json,imported_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    payload,
                )
                inserted.append(item["code"])
    return {"inserted": inserted, "replaced": replaced, "count": len(normalized)}


def list_samples() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, COUNT(b.branch_id) AS branch_count
            FROM samples s LEFT JOIN branches b ON b.sample_id=s.sample_id
            GROUP BY s.sample_id ORDER BY s.code
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_sample(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
    if row is None:
        raise KeyError("未知样本")
    return row


def clear_and_reseed() -> dict[str, Any]:
    with connect() as conn:
        conn.execute("DELETE FROM branches")
        conn.execute("DELETE FROM samples")
        conn.execute("DELETE FROM runs")
        conn.execute("DELETE FROM curves")
    return seed_curves()


def list_runs(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
