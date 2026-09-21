"""固定网格上的放射性碳校准与最高密度区间计算。

约定：
- 日历坐标统一为 cal BP（距今放射性碳年数），内部网格从不反转；
  ``age_direction`` 只控制页面显示方向。
- 曲线在固定网格边上做线性插值，密度定义在固定格子（cell）上，
  归一化与显示缩放完全无关。
- 最高密度范围只按整格纳入，跨过目标概率时记录实际累计质量，
  绝不切分最后一个格子。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import erf, sqrt
from typing import Any, Sequence

import numpy as np

SCHEMA_VERSION = "carbon-branch-path/1"
GRID_MIN = 0.0
GRID_MAX = 1200.0
GRID_STEP = 10.0
COVERAGES: tuple[float, ...] = (0.68, 0.95)
DIRECTIONS = {
    "oldward_positive": "纪年正方向朝古老（cal BP 向右增大）",
    "youngward_positive": "纪年正方向朝年轻（cal BP 向右减小）",
}

EDGES = np.arange(GRID_MIN, GRID_MAX + GRID_STEP / 2.0, GRID_STEP, dtype=float)
CENTERS = (EDGES[:-1] + EDGES[1:]) / 2.0
CELL_COUNT = CENTERS.size


@dataclass(frozen=True)
class Curve:
    version: str
    name: str
    summary: str
    source: str
    knots: tuple[tuple[float, float], ...]
    created_at: str

    @property
    def knot_hash(self) -> str:
        raw = json.dumps(
            {"version": self.version, "knots": [[a, b] for a, b in self.knots]},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def as_summary(self) -> dict[str, Any]:
        ys = [y for _, y in self.knots]
        return {
            "version": self.version,
            "name": self.name,
            "summary": self.summary,
            "source": self.source,
            "created_at": self.created_at,
            "knot_count": len(self.knots),
            "cal_bp_min": min(t for t, _ in self.knots),
            "cal_bp_max": max(t for t, _ in self.knots),
            "c14_min": min(ys),
            "c14_max": max(ys),
            "knot_hash": self.knot_hash,
        }


def curve_from_row(row: dict[str, Any]) -> Curve:
    knots = tuple((float(a), float(b)) for a, b in json.loads(row["knots_json"]))
    return Curve(
        version=row["version"],
        name=row["name"],
        summary=row["summary"],
        source=row["source"],
        knots=knots,
        created_at=row["created_at"],
    )


def interpolate_edges(curve: Curve) -> np.ndarray:
    """在固定网格边上线性插值曲线；曲线必须覆盖整个固定网格。"""
    knot_t = np.array([t for t, _ in curve.knots], dtype=float)
    knot_y = np.array([y for _, y in curve.knots], dtype=float)
    if knot_t.size < 2 or np.any(np.diff(knot_t) <= 0):
        raise ValueError(f"曲线 {curve.version} 的节点必须按 cal BP 严格递增")
    if knot_t[0] > EDGES[0] or knot_t[-1] < EDGES[-1]:
        raise ValueError(f"曲线 {curve.version} 未覆盖固定网格 {GRID_MIN}-{GRID_MAX}")
    return np.interp(EDGES, knot_t, knot_y)


def gaussian_weights(target: float, sigma: float, mu_edges: np.ndarray) -> np.ndarray:
    if sigma <= 0:
        raise ValueError("标准差必须为正数")
    centers = (mu_edges[:-1] + mu_edges[1:]) / 2.0
    z = (centers - target) / sigma
    return np.exp(-0.5 * z * z)


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def truncation_mass(target: float, sigma: float, mu_edges: np.ndarray) -> float:
    """校准曲线 14C 年龄范围之外的实验高斯尾部质量。

    这是固定曲线端点带来的截断诊断，不参与日历密度归一化。
    """
    y_min = float(mu_edges.min())
    y_max = float(mu_edges.max())
    lower = normal_cdf((y_min - target) / sigma)
    upper = 1.0 - normal_cdf((y_max - target) / sigma)
    return float(lower + upper)


def hdr_intervals(
    density: np.ndarray, coverage: float
) -> dict[str, Any]:
    """固定离散网格上的最高密度范围。

    格子按密度降序纳入；同密度保持网格下标升序。最后一个格子使累计
    质量跨过目标概率时停止，返回的 actual_mass 是离散网格的真实质量。
    """
    if not 0.0 < coverage <= 1.0:
        raise ValueError("覆盖概率必须位于 (0, 1]")
    order = np.argsort(-density, kind="stable")
    selected = np.zeros(density.shape, dtype=bool)
    cumulative = 0.0
    threshold = None
    for idx in order:
        selected[idx] = True
        cumulative += float(density[idx])
        threshold = float(density[idx])
        if cumulative + 1e-12 >= coverage:
            break

    intervals: list[dict[str, Any]] = []
    start: int | None = None
    for idx, inside in enumerate(selected):
        if inside and start is None:
            start = idx
        if start is not None and (idx == CELL_COUNT - 1 or not selected[idx + 1]):
            cell_mass = float(density[start : idx + 1].sum())
            intervals.append(
                {
                    "start_bp": float(EDGES[start]),
                    "end_bp": float(EDGES[idx + 1]),
                    "mass": round(cell_mass, 12),
                    "cell_count": idx + 1 - start,
                }
            )
            start = None
    return {
        "target_probability": coverage,
        "actual_mass": round(cumulative, 12),
        "threshold_density": round(float(threshold), 12),
        "intervals": intervals,
    }


def stratified_marginals(
    weights: Sequence[np.ndarray],
) -> list[np.ndarray]:
    """硬分层顺序约束下各测年的边缘后验。

    weights 按 layer 升序排列：layer 小者更年轻（cal BP 更小），
    要求 theta_1 <= theta_2 <= ...（cal BP 非降）。固定网格上用
    前向/后向累积求和完成，不使用加权平均年代。
    """
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [np.asarray(weights[0], dtype=float)]
    if any(float(w.sum()) <= 0.0 for w in weights):
        raise ValueError("存在全零似然，分层先验下质量为零")

    forward = [np.asarray(weights[0], dtype=float)]
    for j in range(1, n):
        younger_or_equal = np.cumsum(forward[j - 1])
        forward.append(np.asarray(weights[j], dtype=float) * younger_or_equal)

    backward = [np.asarray(weights[-1], dtype=float)]
    for j in range(n - 2, -1, -1):
        older_or_equal = np.flip(np.cumsum(np.flip(backward[0])))
        backward.insert(0, np.asarray(weights[j], dtype=float) * older_or_equal)

    marginals: list[np.ndarray] = []
    for j in range(n):
        score = forward[j] * backward[j] / weights[j]
        total = float(score.sum())
        if total <= 0.0:
            raise ValueError("分层先验与所有测年密度不相容（顺序约束下质量为零）")
        marginals.append(score / total)
    return marginals


def grid_metadata() -> dict[str, Any]:
    return {
        "min_bp": GRID_MIN,
        "max_bp": GRID_MAX,
        "step": GRID_STEP,
        "edge_count": int(EDGES.size),
        "cell_count": int(CELL_COUNT),
    }


def _cdf(density: np.ndarray) -> np.ndarray:
    return np.concatenate(([0.0], np.cumsum(density)))


def build_uniform_result(
    curve: Curve,
    sample: dict[str, Any],
    direction: str,
) -> dict[str, Any]:
    reservoir = float(sample.get("reservoir") or 0.0)
    target = float(sample["age"]) - reservoir
    sigma = float(sample["sd"])
    mu_edges = interpolate_edges(curve)
    weights = gaussian_weights(target, sigma, mu_edges)
    unnorm = float(weights.sum())
    if unnorm <= 0.0:
        raise ValueError("固定网格上似然质量为零")
    density = weights / unnorm
    trunc = truncation_mass(target, sigma, mu_edges)
    return _assemble_result(
        curve=curve,
        sample=sample,
        direction=direction,
        density=density,
        target=target,
        sigma=sigma,
        unnorm=unnorm,
        trunc=trunc,
        prior={
            "kind": "uniform",
            "label": "均匀日历先验（每个固定网格格等权）",
            "ordered_samples": [sample["code"]],
        },
    )


def build_stratified_results(
    curve: Curve,
    ordered_samples: Sequence[dict[str, Any]],
    direction: str,
) -> list[dict[str, Any]]:
    if len(ordered_samples) < 2:
        raise ValueError("分层先验至少需要两个测年")
    layers = [int(s["layer"]) for s in ordered_samples if s.get("layer") is not None]
    if len(layers) != len(ordered_samples) or len(set(layers)) != len(layers):
        raise ValueError("分层先验要求每个样本拥有唯一且非空的 layer")
    mu_edges = interpolate_edges(curve)
    prepared: list[dict[str, Any]] = []
    for sample in ordered_samples:
        reservoir = float(sample.get("reservoir") or 0.0)
        target = float(sample["age"]) - reservoir
        sigma = float(sample["sd"])
        weights = gaussian_weights(target, sigma, mu_edges)
        if float(weights.sum()) <= 0.0:
            raise ValueError(f"测年 {sample['code']} 在固定网格上似然质量为零")
        prepared.append(
            {
                "sample": sample,
                "target": target,
                "sigma": sigma,
                "weights": weights,
                "unnorm": float(weights.sum()),
                "trunc": truncation_mass(target, sigma, mu_edges),
            }
        )

    marginals = stratified_marginals([item["weights"] for item in prepared])
    order = [
        {
            "code": item["sample"]["code"],
            "layer": int(item["sample"]["layer"]),
        }
        for item in prepared
    ]
    common_prior = {
        "kind": "stratified",
        "label": "分层硬顺序先验：layer 升序 = 年轻→古老（cal BP 非降）",
        "ordered_samples": order,
    }
    results = []
    for item, density in zip(prepared, marginals):
        prior = dict(common_prior)
        prior["current_code"] = item["sample"]["code"]
        results.append(
            _assemble_result(
                curve=curve,
                sample=item["sample"],
                direction=direction,
                density=density,
                target=item["target"],
                sigma=item["sigma"],
                unnorm=item["unnorm"],
                trunc=item["trunc"],
                prior=prior,
            )
        )
    return results


def _assemble_result(
    *,
    curve: Curve,
    sample: dict[str, Any],
    direction: str,
    density: np.ndarray,
    target: float,
    sigma: float,
    unnorm: float,
    trunc: float,
    prior: dict[str, Any],
) -> dict[str, Any]:
    if direction not in DIRECTIONS:
        raise ValueError(f"未知纪年方向：{direction}")
    total = float(density.sum())
    density = density / total
    cdf = _cdf(density)
    hdrs = [hdr_intervals(density, q) for q in COVERAGES]
    return {
        "schema_version": SCHEMA_VERSION,
        "grid": grid_metadata(),
        "curve": {
            "version": curve.version,
            "name": curve.name,
            "knot_hash": curve.knot_hash,
        },
        "direction": {
            "code": direction,
            "label": DIRECTIONS[direction],
            "positive_toward": "older"
            if direction == "oldward_positive"
            else "younger",
        },
        "sample": {
            "code": sample["code"],
            "age": float(sample["age"]),
            "sd": float(sample["sd"]),
            "material": sample["material"],
            "reservoir": float(sample.get("reservoir") or 0.0),
            "layer": None if sample.get("layer") in (None, "") else int(sample["layer"]),
        },
        "effective_target": target,
        "effective_sigma": sigma,
        "prior": prior,
        "unnormalized_grid_mass": round(unnorm, 12),
        "truncation_mass": round(trunc, 12),
        "density_centers_bp": [float(x) for x in CENTERS],
        "density": [round(float(x), 12) for x in density],
        "cdf_edges_bp": [float(x) for x in EDGES],
        "cdf": [round(float(x), 12) for x in cdf],
        "hdr": hdrs,
    }


def replay_hash(result: dict[str, Any]) -> str:
    """只依赖校准输入与完整密度结果，与数据库主键、时间戳无关。"""
    core = {
        "schema_version": result["schema_version"],
        "grid": result["grid"],
        "curve": result["curve"],
        "direction": result["direction"]["code"],
        "sample": result["sample"],
        "effective_target": result["effective_target"],
        "effective_sigma": result["effective_sigma"],
        "prior": result["prior"],
        "truncation_mass": result["truncation_mass"],
        "density": result["density"],
        "hdr": result["hdr"],
    }
    raw = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
