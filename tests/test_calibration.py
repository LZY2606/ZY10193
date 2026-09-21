import json
from pathlib import Path

import numpy as np
import pytest

from app import calibration as cal

ROOT = Path(__file__).resolve().parent.parent


def load_curve(version: str) -> cal.Curve:
    payload = json.loads((ROOT / "fixtures" / "curves.json").read_text())
    item = next(c for c in payload["curves"] if c["version"] == version)
    return cal.Curve(
        item["version"], item["name"], item["summary"], item["source"],
        tuple((a, b) for a, b in item["knots"]), item["created_at"],
    )


def sample(code="S-A", **over):
    data = {"code": code, "age": 800, "sd": 40, "material": "charcoal",
            "reservoir": 0.0, "layer": None}
    data.update(over)
    return data


def test_grid_is_fixed():
    assert cal.EDGES[0] == 0
    assert cal.EDGES[-1] == 1200
    assert np.allclose(np.diff(cal.EDGES), 10)
    assert cal.CELL_COUNT == 120


def test_v1_density_has_three_separated_peaks_and_discrete_hdr():
    curve = load_curve("local-synthetic-v1")
    result = cal.build_uniform_result(curve, sample(), "oldward_positive")
    density = np.array(result["density"])
    assert density.shape == (120,)
    assert density.sum() == pytest.approx(1.0, abs=1e-10)
    assert result["cdf"][-1] == pytest.approx(1.0, abs=1e-10)

    by_coverage = {h["target_probability"]: h for h in result["hdr"]}
    h68 = by_coverage[0.68]
    assert len(h68["intervals"]) == 3
    assert h68["actual_mass"] == pytest.approx(0.682894309934, abs=1e-9)
    component_sum = sum(i["mass"] for i in h68["intervals"])
    assert component_sum == pytest.approx(h68["actual_mass"], abs=1e-10)

    h95 = by_coverage[0.95]
    assert len(h95["intervals"]) == 3
    assert h95["actual_mass"] >= 0.95
    assert sum(i["mass"] for i in h95["intervals"]) == pytest.approx(
        h95["actual_mass"], abs=1e-10
    )
    for h in (h68, h95):
        for interval in h["intervals"]:
            assert interval["start_bp"] % 10 == 0
            assert interval["end_bp"] % 10 == 0
            assert interval["cell_count"] * 10 == (
                interval["end_bp"] - interval["start_bp"]
            )


def test_last_cell_is_not_split():
    raw = np.array([0.05] * 4 + [0.32] * 2 + [0.02] * (120 - 6))
    density = raw / raw.sum()
    hdr = cal.hdr_intervals(density, 0.18)
    # 第一高峰格约 0.103，未达 0.18；第二格纳入后达到真实 0.205，
    # 记录实际质量而不是把第二格切成 0.18。
    assert hdr["actual_mass"] == pytest.approx(0.64 / 3.12, abs=1e-12)
    assert hdr["intervals"][0]["cell_count"] == 2
    assert hdr["intervals"][0]["mass"] == pytest.approx(0.64 / 3.12, abs=1e-12)


def test_direction_changes_metadata_not_density():
    curve = load_curve("local-synthetic-v1")
    old = cal.build_uniform_result(curve, sample(), "oldward_positive")
    young = cal.build_uniform_result(curve, sample(), "youngward_positive")
    assert old["density"] == young["density"]
    assert old["direction"]["positive_toward"] == "older"
    assert young["direction"]["positive_toward"] == "younger"


def test_reservoir_effective_target_and_truncation():
    curve = load_curve("local-synthetic-v1")
    result = cal.build_uniform_result(
        curve, sample("S-C", age=760, sd=35, reservoir=40),
        "oldward_positive",
    )
    assert result["effective_target"] == 720
    assert result["sample"]["reservoir"] == 40

    boundary = cal.build_uniform_result(
        curve, sample("S-D", age=640, sd=50), "oldward_positive"
    )
    mu_edges = cal.interpolate_edges(curve)
    expected = cal.normal_cdf((mu_edges.min() - 640) / 50) + 1 - cal.normal_cdf(
        (mu_edges.max() - 640) / 50
    )
    assert boundary["truncation_mass"] == pytest.approx(expected, abs=1e-10)
    assert boundary["truncation_mass"] > 0.6


def test_stratified_marginals_keep_order_and_full_distributions():
    curve = load_curve("local-synthetic-v1")
    ordered = [
        sample("T1", age=800, sd=40, layer=1),
        sample("T2", age=820, sd=40, layer=2),
        sample("T3", age=760, sd=35, reservoir=40, layer=3),
    ]
    results = cal.build_stratified_results(
        curve, ordered, "oldward_positive"
    )
    assert len(results) == 3
    means = []
    for result in results:
        d = np.array(result["density"])
        assert d.sum() == pytest.approx(1, abs=1e-10)
        assert result["prior"]["kind"] == "stratified"
        means.append(float((d * np.array(result["density_centers_bp"])).sum()))
    assert means == sorted(means)


def test_stratified_impossible_layer_rejected():
    curve = load_curve("local-synthetic-v1")
    with pytest.raises(ValueError):
        cal.build_stratified_results(
            curve,
            [sample("A", layer=1), sample("B", layer=1)],
            "oldward_positive",
        )


def test_replay_hash_stable():
    curve = load_curve("local-synthetic-v1")
    r1 = cal.build_uniform_result(curve, sample(), "oldward_positive")
    r2 = cal.build_uniform_result(curve, sample(), "oldward_positive")
    assert cal.replay_hash(r1) == cal.replay_hash(r2)
