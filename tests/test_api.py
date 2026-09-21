import io
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent


def upload_samples(client):
    data = {"file": ("samples.csv", (ROOT / "fixtures" / "samples.csv").read_bytes(),
                     "text/csv")}
    return client.post("/api/import", files=data)


def create_branch(client, code="S-A", version="local-synthetic-v1",
                  direction="oldward_positive"):
    return client.post(
        "/api/branches",
        json={"code": code, "curve_version": version, "direction": direction},
    )


def test_root_shows_chinese_title(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "碳纪枝径" in response.text


def test_curves_are_seeded_and_distinct(client):
    curves = client.get("/api/curves").json()
    versions = {c["version"] for c in curves}
    assert versions == {"local-synthetic-v1", "local-synthetic-v2"}
    hashes = {c["knot_hash"] for c in curves}
    assert len(hashes) == 2


def test_import_and_branch_actual_hdr_mass(client):
    assert upload_samples(client).json()["imported"] == 7
    response = create_branch(client)
    assert response.status_code == 200
    result = response.json()["result"]
    h68 = next(h for h in result["hdr"] if h["target_probability"] == 0.68)
    assert len(h68["intervals"]) == 3
    assert abs(sum(i["mass"] for i in h68["intervals"]) - h68["actual_mass"]) < 1e-9
    assert result["direction"]["code"] == "oldward_positive"
    assert result["grid"]["cell_count"] == 120


def test_switching_version_keeps_old_branch_and_never_merges(client):
    upload_samples(client)
    b1 = create_branch(client, version="local-synthetic-v1").json()["branch_id"]
    b2 = create_branch(client, version="local-synthetic-v2").json()["branch_id"]
    assert b1 != b2
    branches = client.get("/api/branches").json()
    assert len(branches) == 2
    assert {b["curve_version"] for b in branches} == {
        "local-synthetic-v1", "local-synthetic-v2"
    }
    r1 = client.get(f"/api/branches/{b1}").json()["result"]
    r2 = client.get(f"/api/branches/{b2}").json()["result"]
    assert r1["curve"]["version"] == "local-synthetic-v1"
    assert r2["curve"]["version"] == "local-synthetic-v2"
    assert r1["density"] != r2["density"]
    assert client.post(
        "/api/branches",
        json={"code": "S-A", "curve_version": "unknown",
              "direction": "oldward_positive"},
    ).status_code in (400, 404)


def test_direction_is_stored_and_visible_but_density_unchanged(client):
    upload_samples(client)
    old = create_branch(client, direction="oldward_positive").json()["result"]
    young = create_branch(client, direction="youngward_positive").json()["result"]
    assert old["density"] == young["density"]
    assert old["direction"]["positive_toward"] == "older"
    assert young["direction"]["positive_toward"] == "younger"


def test_stratified_group_creates_ordered_branches(client):
    upload_samples(client)
    response = client.post(
        "/api/branches/stratified",
        json={
            "codes": ["T3", "T1", "T2"],
            "curve_version": "local-synthetic-v1",
            "direction": "oldward_positive",
        },
    )
    assert response.status_code == 200
    payload = response.json()["branches"]
    assert len(payload) == 3
    prior = payload[0]["result"]["prior"]
    assert [x["code"] for x in prior["ordered_samples"]] == ["T1", "T2", "T3"]
    means = []
    for item in payload:
        result = item["result"]
        d = np.array(result["density"])
        means.append(float((d * np.array(result["density_centers_bp"])).sum()))
    assert means == sorted(means)
    group_keys = {
        b["group_key"]
        for b in client.get("/api/branches").json()
        if b["prior_kind"] == "stratified"
    }
    assert len(group_keys) == 1


def test_missing_layer_rejected(client):
    upload_samples(client)
    response = client.post(
        "/api/branches/stratified",
        json={"codes": ["S-A", "S-B"], "curve_version": "local-synthetic-v1",
              "direction": "oldward_positive"},
    )
    assert response.status_code == 400


def test_replay_matches(client):
    upload_samples(client)
    branch_id = create_branch(client).json()["branch_id"]
    replay = client.post(f"/api/branches/{branch_id}/replay").json()
    assert replay["match"] is True
    assert replay["stored_hash"] == replay["replayed_hash"]


def test_export_reset_import_and_replay(client):
    upload_samples(client)
    create_branch(client)
    create_branch(client, version="local-synthetic-v2")
    client.post(
        "/api/branches/stratified",
        json={"codes": ["T1", "T2", "T3"],
              "curve_version": "local-synthetic-v1",
              "direction": "oldward_positive"},
    )
    bundle = client.get("/api/export").json()
    assert bundle["export_schema"] == "carbon-branch-path/export/1"
    assert len(bundle["samples"]) == 7
    assert len(bundle["branches"]) == 5
    assert len(bundle["results"]) == 5
    assert len(bundle["curves"]) == 2

    assert client.post("/api/reset").status_code == 200
    assert client.get("/api/samples").json() == []
    assert len(client.get("/api/curves").json()) == 2

    files = {"file": ("runs.json", json.dumps(bundle).encode(), "application/json")}
    restored = client.post("/api/import-runs", files=files).json()
    assert restored["imported"]["branches"] == 5
    replay = client.post("/api/branches/1/replay").json()
    assert replay["match"] is True
    detail = client.get("/api/branches/1").json()
    assert detail["result"]["curve"]["version"] == "local-synthetic-v1"


def test_import_runs_rejects_nonempty_workspace(client):
    upload_samples(client)
    bundle = client.get("/api/export").json()
    files = {"file": ("runs.json", json.dumps(bundle).encode(), "application/json")}
    response = client.post("/api/import-runs", files=files)
    assert response.status_code == 409


def test_tampered_export_is_rejected(client):
    upload_samples(client)
    create_branch(client)
    bundle = client.get("/api/export").json()
    client.post("/api/reset")
    bundle["results"][0]["result_hash"] = "tampered"
    files = {"file": ("bad.json", json.dumps(bundle).encode(), "application/json")}
    response = client.post("/api/import-runs", files=files)
    assert response.status_code == 400
