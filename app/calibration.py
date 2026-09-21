from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

GRID_START = 0
GRID_STOP = 12000
GRID_STEP = 10

EDGES = np.linspace(GRID_START, GRID_STOP, int((GRID_STOP - GRID_START) / GRID_STEP) + 1, dtype=np.float64)
CENTERS = (EDGES[:-1] + EDGES[1:]) / 2.0
CELL_WIDTH = np.float64(GRID_STEP)

MATERIAL_ALIASES = {
    "charcoal": "charcoal",
    "wood": "wood",
    "plant": "plant",
    "bone": "bone",
    "terrestrial": "terrestrial",
    "shell": "shell",
    "marine shell": "shell",
    "marine": "marine",
    "fish": "fish",
    "peat": "peat",
    "木炭": "charcoal",
    "木材": "wood",
    "植物": "plant",
    "骨头": "bone",
    "贝壳": "shell",
    "海洋贝壳": "shell",
    "泥炭": "peat",
}

PRIOR_VERSIONS = {
    "uniform-v1": {
        "key": "uniform-v1",
        "label": "全局均匀先验 v1",
        "kind": "uniform",
        "sources": [{"name": "固定日历支撑域", "detail": f"{GRID_START}-{GRID_STOP} cal BP，密度恒定"}],
    },
    "stratified-v1": {
        "key": "stratified-v1",
        "label": "分层先验 v1",
        "kind": "stratified",
        "layer_weight": 0.7,
        "background_weight": 0.3,
        "layers": [
            {"key": "terrestrial", "label": "陆生短周期材料层", "start": 4600, "stop": 5600},
            {"key": "reservoir", "label": "库效应材料校正层", "start": 4400, "stop": 5500},
            {"key": "unassigned", "label": "未归属材料层", "start": 4200, "stop": 5800},
        ],
        "sources": [
            {"name": "LOCAL-STRATA-v1", "detail": "固定夹具：0.7 层内均匀 + 0.3 全支撑域均匀"},
            {"name": "材料映射", "detail": "charcoal/wood/plant/bone/peat→terrestrial；shell/marine/fish→reservoir"},
        ],
    },
}


def canonical_material(material: str | None) -> str:
    normalized = (material or "unassigned").strip().lower()
    return MATERIAL_ALIASES.get(normalized, normalized or "unassigned")


def curve_fingerprint(points: list[dict[str, float]]) -> str:
    payload = json.dumps(points, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_curve_csv(path: str | Path) -> tuple[list[dict[str, float]], str]:
    rows: list[dict[str, float]] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        import csv

        for raw in csv.DictReader(handle):
            rows.append(
                {
                    "calendar_bp": float(raw["calendar_bp"]),
                    "c14_age": float(raw["c14_age"]),
                    "c14_sigma": float(raw["c14_sigma"]),
                }
            )
    return rows, curve_fingerprint(rows)


def interpolate_curve(points: list[dict[str, float]]) -> tuple[np.ndarray, np.ndarray]:
    knots = np.asarray([(row["calendar_bp"], row["c14_age"], row["c14_sigma"]) for row in points], dtype=np.float64)
    order = np.argsort(knots[:, 0])
    knots = knots[order]
    ages = np.interp(CENTERS, knots[:, 0], knots[:, 1])
    sigmas = np.interp(CENTERS, knots[:, 0], knots[:, 2])
    return ages, sigmas


def prior_profile(prior_key: str | None, material: str | None = None) -> dict[str, Any]:
    key = prior_key or "uniform-v1"
    if key not in PRIOR_VERSIONS:
        raise ValueError("未知先验版本")
    profile = json.loads(json.dumps(PRIOR_VERSIONS[key]))
    profile["material"] = canonical_material(material)
    if profile["kind"] == "stratified":
        selected = "reservoir" if profile["material"] in {"shell", "marine", "fish"} else "terrestrial"
        if profile["material"] == "unassigned":
            selected = "unassigned"
        profile["selected_layer"] = next(layer for layer in profile["layers"] if layer["key"] == selected)
    return profile


def prior_density(profile: dict[str, Any]) -> np.ndarray:
    if profile["kind"] == "uniform":
        return np.full_like(CENTERS, 1.0 / (GRID_STOP - GRID_START), dtype=np.float64)
    layer = profile["selected_layer"]
    density = np.full_like(CENTERS, profile["background_weight"] / (GRID_STOP - GRID_START), dtype=np.float64)
    inside = (CENTERS >= layer["start"]) & (CENTERS <= layer["stop"])
    density[inside] += profile["layer_weight"] / (layer["stop"] - layer["start"])
    return density


def _gaussian_unnormalized(x: np.ndarray | float, mu: float, sigma: float) -> np.ndarray | float:
    return np.exp(-0.5 * ((np.asarray(x, dtype=np.float64) - mu) / sigma) ** 2)


def _gaussian_half_integral(z: float, older: bool) -> float:
    cdf = _norm_cdf(z)
    if older:
        return float(math.sqrt(2.0 * math.pi) * (1.0 - cdf))
    return float(math.sqrt(2.0 * math.pi) * cdf)


def _error_integral(a: float, b: float, c: float, older_side: bool) -> float:
    if a <= 0:
        return 0.0
    scale = math.sqrt(math.pi / a) * math.exp(b * b / (4.0 * a) - c)
    arg = b / (2.0 * math.sqrt(a))
    if older_side:
        return scale * math.erfc(arg) / 2.0
    return scale * (1.0 + math.erf(arg)) / 2.0


def _linear_gaussian_tail(
    curve_age_at_edge: float,
    slope: float,
    measurement_sigma: float,
    curve_sigma: float,
    target: float,
    older_side: bool,
) -> float:
    age_sigma = math.sqrt(measurement_sigma**2 + curve_sigma**2)
    if abs(slope) < 1e-12:
        z = (curve_age_at_edge - target) / age_sigma
        return age_sigma * _gaussian_half_integral(z, older=older_side)
    residual = target - curve_age_at_edge
    a = 0.5 * (slope / age_sigma) ** 2
    b = slope * residual / age_sigma**2
    c = 0.5 * (residual / age_sigma) ** 2
    return _error_integral(a, b, c, older_side)


def _norm_cdf(value: float) -> float:
    return float(0.5 * (1.0 + math.erf(value / math.sqrt(2.0))))


def extrapolated_tail_mass(
    points: list[dict[str, float]],
    target: float,
    measurement_sigma: float,
    prior: np.ndarray,
) -> dict[str, float]:
    first = points[0]
    first_slope = (points[1]["c14_age"] - first["c14_age"]) / (points[1]["calendar_bp"] - first["calendar_bp"])
    left = _linear_gaussian_tail(
        first["c14_age"], first_slope, measurement_sigma, first["c14_sigma"], target, older_side=False
    ) * float(prior[0])
    last = points[-1]
    previous = points[-2]
    slope = (last["c14_age"] - previous["c14_age"]) / (last["calendar_bp"] - previous["calendar_bp"])
    right = _linear_gaussian_tail(
        last["c14_age"], slope, measurement_sigma, last["c14_sigma"], target, older_side=True
    ) * float(prior[-1])
    return {"left_bp0": left, "right_bp12000": right, "total": left + right}


@dataclass
class CalibrationInput:
    age_bp: float
    sigma_bp: float
    material: str = "unassigned"
    reservoir_correction: float = 0.0
    prior_key: str = "uniform-v1"


def calibrate(points: list[dict[str, float]], measurement: CalibrationInput) -> dict[str, Any]:
    if measurement.sigma_bp <= 0:
        raise ValueError("标准差必须为正数")
    curve_age, curve_sigma = interpolate_curve(points)
    target = measurement.age_bp - measurement.reservoir_correction
    total_sigma = np.sqrt(measurement.sigma_bp**2 + curve_sigma**2)
    likelihood = _gaussian_unnormalized(curve_age, target, total_sigma)
    profile = prior_profile(measurement.prior_key, measurement.material)
    prior = prior_density(profile)
    unnormalized = likelihood * prior
    masses = unnormalized * CELL_WIDTH
    normalization = float(masses.sum())
    if normalization <= 0:
        raise ValueError("固定网格上没有概率质量")
    cell_mass = masses / normalization
    density = cell_mass / CELL_WIDTH
    cdf = np.concatenate(([0.0], np.cumsum(cell_mass)))
    trap_mass = np.trapezoid(unnormalized, CENTERS) if hasattr(np, "trapezoid") else np.trapz(unnormalized, CENTERS)
    tail_raw = extrapolated_tail_mass(points, target, float(measurement.sigma_bp), prior)
    return {
        "edges_bp": EDGES,
        "centers_bp": CENTERS,
        "curve_age_bp": curve_age,
        "curve_sigma_bp": curve_sigma,
        "likelihood": likelihood,
        "prior_density": prior,
        "prior_profile": profile,
        "density": density,
        "cell_mass": cell_mass,
        "cdf": cdf,
        "normalization": normalization,
        "effective_target_bp": target,
        "total_sigma_bp": total_sigma,
        "quadrature_residual_years": abs(float(masses.sum() - trap_mass) / normalization),
        "analytic_tail_mass": {key: value / normalization for key, value in tail_raw.items()},
    }


def highest_density_regions(cell_mass: np.ndarray, density: np.ndarray, coverage: float) -> dict[str, Any]:
    if not 0.0 < coverage <= 1.0:
        raise ValueError("覆盖概率必须位于 (0,1]")
    order = np.argsort(density)[::-1]
    cumulative = 0.0
    included = np.zeros_like(density, dtype=bool)
    cutoff = None
    crossed_index = None
    for index in order:
        if cumulative >= coverage and density[index] != cutoff:
            break
        included[index] = True
        cumulative += float(cell_mass[index])
        cutoff = float(density[index])
        crossed_index = int(index)
        if cumulative >= coverage:
            break
    intervals: list[dict[str, Any]] = []
    active = None
    for index, flag in enumerate(included):
        if flag and active is None:
            active = [index, index]
        elif flag:
            active[1] = index
        elif active is not None:
            intervals.append(_interval_payload(active, cell_mass, density))
            active = None
    if active is not None:
        intervals.append(_interval_payload(active, cell_mass, density))
    return {
        "requested_probability": coverage,
        "actual_probability": float(cell_mass[included].sum()),
        "density_cutoff": float(cutoff),
        "crossed_cell_center_bp": float(CENTERS[crossed_index]),
        "cell_split": False,
        "interval_count": len(intervals),
        "intervals": intervals,
    }


def _interval_payload(span: list[int], cell_mass: np.ndarray, density: np.ndarray) -> dict[str, Any]:
    start_index, stop_index = span
    return {
        "start_bp": float(EDGES[start_index]),
        "stop_bp": float(EDGES[stop_index + 1]),
        "mass": float(cell_mass[start_index : stop_index + 1].sum()),
        "peak_center_bp": float(CENTERS[start_index + int(np.argmax(density[start_index : stop_index + 1]))]),
        "cell_count": int(stop_index - start_index + 1),
        "touches_grid_start": bool(start_index == 0),
        "touches_grid_stop": bool(stop_index == len(cell_mass) - 1),
    }


def forward_younger(value_bp: float) -> float:
    return 1950.0 - value_bp


def direction_summary(direction: str) -> dict[str, str]:
    if direction == "BP_OLDER_POSITIVE":
        return {"key": direction, "label": "cal BP，向过去/更老为正", "axis_label": "日历年 BP（越右越老）"}
    if direction == "FORWARD_YOUNGER_POSITIVE":
        return {"key": direction, "label": "公元纪年，向年轻/公元后为正", "axis_label": "日历年（越右越年轻；负值=BCE，正值=CE）"}
    raise ValueError("未知纪年方向")


def intervals_in_direction(intervals: list[dict[str, Any]], direction: str) -> list[dict[str, Any]]:
    result = []
    for interval in intervals:
        row = dict(interval)
        if direction == "FORWARD_YOUNGER_POSITIVE":
            row["start_display"] = forward_younger(interval["stop_bp"])
            row["stop_display"] = forward_younger(interval["start_bp"])
        else:
            row["start_display"] = interval["start_bp"]
            row["stop_display"] = interval["stop_bp"]
        result.append(row)
    return result
