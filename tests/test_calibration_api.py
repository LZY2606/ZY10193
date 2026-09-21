from __future__ import annotations


def create_fixed_branch(client, curve_id="LOCAL-13k-v1", coverages=None, prior_key="uniform-v1"):
    client.post("/api/import/fixture")
    sample = client.get("/api/samples").json()[0]
    payload = {
        "curve_id": curve_id,
        "prior_key": prior_key,
        "direction": "BP_OLDER_POSITIVE",
        "coverages": coverages or [0.683, 0.954],
    }
    response = client.post(f"/api/samples/{sample['sample_id']}/branches", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["branch"]


def test_root_title_and_curves(client):
    assert "碳纪枝径" in client.get("/").text
    curves = client.get("/api/curves").json()
    assert [curve["version"] for curve in curves] == ["LOCAL-13k-v1", "LOCAL-13k-v2"]
    assert all(curve["grid_step_years"] == 10 for curve in curves)


def test_three_peaks_and_two_coverage_actual_masses(client):
    branch = create_fixed_branch(client)
    result = branch["result"]
    assert abs(sum(result["posterior_cell_mass"]) - 1.0) < 1e-12
    regions = result["highest_density_regions"]
    assert [region["requested_probability"] for region in regions] == [0.683, 0.954]
    assert [region["interval_count"] for region in regions] == [3, 3]
    for region in regions:
        assert region["cell_split"] is False
        assert region["actual_probability"] >= region["requested_probability"]
        assert region["actual_probability"] - region["requested_probability"] <= max(result["posterior_cell_mass"]) + 1e-12
        assert abs(sum(interval["mass"] for interval in region["intervals"]) - region["actual_probability"]) < 1e-12
        assert all(interval["start_bp"] % 10 == 0 and interval["stop_bp"] % 10 == 0 for interval in region["intervals"])


def test_direction_is_stored_and_display_intervals_reversed(client):
    client.post("/api/import/fixture")
    sample = client.get("/api/samples").json()[0]
    payload = {"curve_id": "LOCAL-13k-v1", "prior_key": "uniform-v1", "direction": "FORWARD_YOUNGER_POSITIVE", "coverages": [0.954]}
    branch = client.post(f"/api/samples/{sample['sample_id']}/branches", json=payload).json()["branch"]
    assert branch["direction"]["key"] == "FORWARD_YOUNGER_POSITIVE"
    assert "向年轻" in branch["direction"]["label"]
    interval = branch["result"]["highest_density_regions"][0]["intervals"][0]
    assert interval["start_display"] == 1950 - interval["stop_bp"]
    assert interval["stop_display"] == 1950 - interval["start_bp"]


def test_curve_versions_remain_separate_replayable_branches(client):
    first = create_fixed_branch(client, "LOCAL-13k-v1")
    second = create_fixed_branch(client, "LOCAL-13k-v2")
    assert first["branch_id"] != second["branch_id"]
    assert first["curve"]["fingerprint_sha256"] != second["curve"]["fingerprint_sha256"]
    collection = client.get("/api/branches").json()
    assert collection["merge_policy"] == "different_curve_versions_never_merged"
    assert collection["weighted_mean_replacement"] is False
    assert len(collection["branches"]) == 2
    replay = client.post(f"/api/branches/{first['branch_id']}/replay").json()
    assert replay["replayed"] is True
    assert replay["branch"]["branch_id"] == first["branch_id"]


def test_stratified_prior_sources_and_reservoir_effective_target(client):
    branch = create_fixed_branch(client, coverages=[0.683], prior_key="stratified-v1")
    profile = branch["result"]["prior_profile"]
    assert profile["selected_layer"]["key"] == "terrestrial"
    assert profile["sources"][0]["name"] == "LOCAL-STRATA-v1"
    assert abs(branch["effective_target_bp"] - 5000.0) < 1e-12
    samples = client.get("/api/samples").json()
    shell = next(sample for sample in samples if sample["code"] == "FIX-B")
    payload = {"curve_id": "LOCAL-13k-v1", "prior_key": "stratified-v1", "direction": "BP_OLDER_POSITIVE", "coverages": [0.683]}
    shell_branch = client.post(f"/api/samples/{shell['sample_id']}/branches", json=payload).json()["branch"]
    assert shell_branch["effective_target_bp"] == 4880
    assert shell_branch["result"]["prior_profile"]["selected_layer"]["key"] == "reservoir"


def test_clear_reseed_then_import_and_run_export(client):
    create_fixed_branch(client)
    cleared = client.post("/api/admin/clear").json()
    assert cleared["cleared"] is True
    assert len(cleared["curves_reseeded"]["inserted"]) == 2
    assert client.get("/api/samples").json() == []
    assert client.get("/api/branches").json()["branches"] == []
    client.post("/api/import/fixture")
    assert len(client.get("/api/samples").json()) == 2
    runs = client.get("/api/runs").json()
    assert any(run["action"] == "admin_clear" for run in runs)
    exported = client.get("/api/runs/export")
    assert "admin_clear" in exported.text and exported.headers["content-disposition"].startswith("attachment")


def test_csv_import_chinese_aliases(client):
    csv_text = "编号,实验年龄,误差,材料,库效应\n中文样,5000,35,木炭,0\n"
    response = client.post("/api/import", json={"csv_text": csv_text, "source": "chinese"})
    assert response.status_code == 200
    sample = client.get("/api/samples").json()[0]
    assert sample["code"] == "中文样"
    assert sample["material"] == "charcoal"
