from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from . import db
from .calibration import (
    CalibrationInput,
    calibrate,
    direction_summary,
    highest_density_regions,
    intervals_in_direction,
)


def _array(values: np.ndarray) -> list[float]:
    return [float(value) for value in values]


def default_coverages() -> list[float]:
    return [0.683, 0.954]


def validate_direction(direction: str | None) -> str:
    direction = direction or "BP_OLDER_POSITIVE"
    direction_summary(direction)
    return direction


def validate_coverages(coverages: Any) -> list[float]:
    if coverages in (None, "", []):
        values = default_coverages()
    else:
        if isinstance(coverages, str):
            coverages = [item.strip() for item in coverages.split(",") if item.strip()]
        values = [float(item) for item in coverages]
    if not 1 <= len(values) <= 2 or any(not 0 < value <= 1 for value in values):
        raise ValueError("coverages 需要 1-2 个位于 (0,1] 的概率")
    return values


def branch_fingerprint(sample: dict[str, Any], curve_fingerprint: str, prior_key: str, coverages: list[float]) -> str:
    payload = {
        "code": sample["code"],
        "age_bp": sample["age_bp"],
        "sigma_bp": sample["sigma_bp"],
        "material": sample["material"],
        "reservoir_correction": sample["reservoir_correction"],
        "curve_fingerprint": curve_fingerprint,
        "prior_key": prior_key,
        "coverages": coverages,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def serialize_result(calibration: dict[str, Any], coverages: list[float], direction: str) -> dict[str, Any]:
    regions = []
    for coverage in coverages:
        region = highest_density_regions(calibration["cell_mass"], calibration["density"], coverage)
        region["intervals"] = intervals_in_direction(region["intervals"], direction)
        region["direction"] = direction_summary(direction)
        regions.append(region)
    edge_mass = float(calibration["cell_mass"][0] + calibration["cell_mass"][-1])
    truncation = {
        "grid_start_bp": 0,
        "grid_stop_bp": 12000,
        "fixed_grid_step_years": 10,
        "edge_cell_mass": edge_mass,
        "analytic_outside_curve_tail_mass": calibration["analytic_tail_mass"]["total"],
        "left_tail_mass": calibration["analytic_tail_mass"]["left_bp0"],
        "right_tail_mass": calibration["analytic_tail_mass"]["right_bp12000"],
        "midpoint_vs_linear_grid_residual": calibration["quadrature_residual_years"],
        "truncated_at_grid_edge": bool(any(i["touches_grid_start"] or i["touches_grid_stop"] for r in regions for i in r["intervals"])),
        "note": "概率只在固定网格内归一化；诊断值不改变实际离散网格质量。",
    }
    return {
        "grid": {
            "start_bp": 0,
            "stop_bp": 12000,
            "step_years": 10,
            "cell_count": len(calibration["cell_mass"]),
        },
        "effective_target_bp": calibration["effective_target_bp"],
        "total_sigma_bp": _array(calibration["total_sigma_bp"]),
        "edges_bp": _array(calibration["edges_bp"]),
        "centers_bp": _array(calibration["centers_bp"]),
        "curve_age_bp": _array(calibration["curve_age_bp"]),
        "curve_sigma_bp": _array(calibration["curve_sigma_bp"]),
        "likelihood_unnormalized": _array(calibration["likelihood"]),
        "prior_density": _array(calibration["prior_density"]),
        "posterior_density": _array(calibration["density"]),
        "posterior_cell_mass": _array(calibration["cell_mass"]),
        "cdf_on_edges": _array(calibration["cdf"]),
        "normalization_unnormalized": calibration["normalization"],
        "prior_profile": calibration["prior_profile"],
        "highest_density_regions": regions,
        "truncation": truncation,
    }


def create_or_replay_branch(
    sample_id: int,
    curve_id: str,
    prior_key: str | None = None,
    direction: str | None = None,
    coverages: Any = None,
) -> dict[str, Any]:
    prior_key = prior_key or "uniform-v1"
    direction = validate_direction(direction)
    coverage_values = validate_coverages(coverages)
    with db.connect() as conn:
        sample = dict(db.get_sample(conn, sample_id))
        curve = dict(db.get_curve(conn, curve_id))
        points = json.loads(curve["points_json"])
        coverages_key = json.dumps(coverage_values, separators=(",", ":"))
        existing = conn.execute(
            """
            SELECT * FROM branches
            WHERE sample_id=? AND curve_id=? AND prior_key=? AND direction=? AND coverages_json=?
            """,
            (sample_id, curve_id, prior_key, direction, coverages_key),
        ).fetchone()
        replayed = existing is not None
        if existing:
            return {"replayed": True, "branch": public_branch(dict(existing), curve)}
        measurement = CalibrationInput(
            age_bp=sample["age_bp"],
            sigma_bp=sample["sigma_bp"],
            material=sample["material"],
            reservoir_correction=sample["reservoir_correction"],
            prior_key=prior_key,
        )
        calibration = calibrate(points, measurement)
        result = serialize_result(calibration, coverage_values, direction)
        fingerprint = branch_fingerprint(sample, curve["fingerprint_sha256"], prior_key, coverage_values)
        cursor = conn.execute(
            """
            INSERT INTO branches
            (sample_id,curve_id,prior_key,direction,coverages_json,fingerprint_sha256,
             effective_target_bp,normalization,prior_profile_json,result_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sample_id,
                curve_id,
                prior_key,
                direction,
                coverages_key,
                fingerprint,
                result["effective_target_bp"],
                result["normalization_unnormalized"],
                json.dumps(result["prior_profile"], ensure_ascii=False, sort_keys=True),
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                db.utc_now(),
            ),
        )
        branch_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
    payload = {"sample_id": sample_id, "curve_id": curve_id, "prior_key": prior_key, "direction": direction, "coverages": coverage_values}
    db.record_run("branch_replay" if replayed else "branch_create", f"branch:{branch_id}", payload, "ok", {"replayed": replayed})
    return {"replayed": False, "branch": public_branch(dict(row), curve)}


def public_branch(row: dict[str, Any], curve: dict[str, Any] | None = None) -> dict[str, Any]:
    result = json.loads(row["result_json"])
    return {
        "branch_id": row["branch_id"],
        "sample_id": row["sample_id"],
        "curve": {
            "curve_id": row["curve_id"],
            "version": curve["version"] if curve else row["curve_id"],
            "fingerprint_sha256": curve["fingerprint_sha256"] if curve else None,
        },
        "prior_key": row["prior_key"],
        "direction": direction_summary(row["direction"]),
        "coverages": json.loads(row["coverages_json"]),
        "fingerprint_sha256": row["fingerprint_sha256"],
        "effective_target_bp": row["effective_target_bp"],
        "created_at": row["created_at"],
        "prior_profile": json.loads(row["prior_profile_json"]),
        "result": result,
    }


def list_branches(sample_id: int | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT b.*, c.version, c.fingerprint_sha256 AS curve_fingerprint
        FROM branches b JOIN curves c ON c.curve_id=b.curve_id
    """
    params: tuple[Any, ...] = ()
    if sample_id is not None:
        query += " WHERE b.sample_id=? ORDER BY b.branch_id"
        params = (sample_id,)
    else:
        query += " ORDER BY b.sample_id,b.branch_id"
    with db.connect() as conn:
        rows = conn.execute(query, params).fetchall()
    branches = []
    for row in rows:
        data = dict(row)
        branch = public_branch(data, {"version": data["version"], "fingerprint_sha256": data["curve_fingerprint"]})
        branches.append(branch)
    return branches


def get_branch(branch_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT b.*, c.version, c.fingerprint_sha256 AS curve_fingerprint
            FROM branches b JOIN curves c ON c.curve_id=b.curve_id
            WHERE b.branch_id=?
            """,
            (branch_id,),
        ).fetchone()
        if row is None:
            raise KeyError("未知分支")
        data = dict(row)
        return public_branch(data, {"version": data["version"], "fingerprint_sha256": data["curve_fingerprint"]})


def branch_collection() -> dict[str, Any]:
    return {
        "merge_policy": "different_curve_versions_never_merged",
        "weighted_mean_replacement": False,
        "branches": list_branches(),
    }
