"""导入、分支构建、重放、导出等业务逻辑。"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from . import calibration as cal
from .db import get_curve, log_action
from .calibration import Curve, curve_from_row

REQUIRED_COLUMNS = ("code", "age", "sd", "material")
OPTIONAL_COLUMNS = ("reservoir", "layer")


def parse_samples(content: str) -> list[dict[str, Any]]:
    text = content.strip()
    if not text:
        raise ValueError("导入内容为空")
    if text[0] in "[{":
        payload = json.loads(text)
        if isinstance(payload, dict) and "samples" in payload:
            payload = payload["samples"]
        if not isinstance(payload, list):
            raise ValueError("JSON 必须是样本数组或 {'samples': [...]}")
        return [_validate_sample(dict(item)) for item in payload]
    return parse_csv(text)


def parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV 缺少表头")
    missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing:
        raise ValueError("CSV 缺少必需列：" + ", ".join(missing))
    samples: list[dict[str, Any]] = []
    for row in reader:
        clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        samples.append(_validate_sample(clean))
    return samples


def _validate_sample(raw: dict[str, Any]) -> dict[str, Any]:
    missing = [c for c in REQUIRED_COLUMNS if str(raw.get(c, "")).strip() == ""]
    if missing:
        raise ValueError("样本缺少必需字段：" + ", ".join(missing))
    code = str(raw["code"]).strip()
    try:
        age = float(raw["age"])
        sd = float(raw["sd"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"样本 {code} 的年龄/标准差必须是数字") from exc
    if sd <= 0:
        raise ValueError(f"样本 {code} 的标准差必须为正数")
    reservoir = float(raw.get("reservoir") or 0.0)
    layer_raw = raw.get("layer")
    layer = None
    if layer_raw not in (None, "", ):
        try:
            layer = int(float(layer_raw))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"样本 {code} 的 layer 必须是整数") from exc
    material = str(raw["material"]).strip()
    return {
        "code": code,
        "age": age,
        "sd": sd,
        "material": material,
        "reservoir": reservoir,
        "layer": layer,
    }


def upsert_samples(conn, samples: list[dict[str, Any]]) -> dict[str, int]:
    inserted = updated = 0
    for sample in samples:
        existing = conn.execute(
            "SELECT id FROM samples WHERE code=?", (sample["code"],)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO samples(code,age,sd,material,reservoir,layer)"
                " VALUES (?,?,?,?,?,?)",
                (
                    sample["code"], sample["age"], sample["sd"],
                    sample["material"], sample["reservoir"], sample["layer"],
                ),
            )
            inserted += 1
        else:
            conn.execute(
                "UPDATE samples SET age=?,sd=?,material=?,reservoir=?,layer=?"
                " WHERE code=?",
                (
                    sample["age"], sample["sd"], sample["material"],
                    sample["reservoir"], sample["layer"], sample["code"],
                ),
            )
            updated += 1
    log_action(conn, "import", f"导入样本 {inserted} 条，更新 {updated} 条")
    return {"inserted": inserted, "updated": updated}


def load_curve(conn, version: str) -> Curve:
    return curve_from_row(get_curve(conn, version))


def fetch_sample(conn, code: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM samples WHERE code=?", (code,)).fetchone()
    if row is None:
        raise LookupError(code)
    return dict(row)


def create_uniform_branch(
    conn, *, code: str, curve_version: str, direction: str
) -> tuple[int, dict[str, Any]]:
    sample = fetch_sample(conn, code)
    curve = load_curve(conn, curve_version)
    result = cal.build_uniform_result(curve, sample, direction)
    result_hash = cal.replay_hash(result)
    cur = conn.execute(
        "INSERT INTO branches(sample_code,curve_version,direction,prior_kind,group_key)"
        " VALUES (?,?,?,?,?)",
        (code, curve_version, direction, "uniform", None),
    )
    branch_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO results(branch_id,result_json,result_hash) VALUES (?,?,?)",
        (branch_id, json.dumps(result, ensure_ascii=False), result_hash),
    )
    log_action(conn, "branch", f"均匀先验分支 #{branch_id}: {code} @ {curve_version}", branch_id)
    return branch_id, result


def _group_key(curve_version: str, direction: str, codes: list[str]) -> str:
    return "|".join([curve_version, direction] + codes)


def create_stratified_branches(
    conn, *, codes: list[str], curve_version: str, direction: str
) -> list[tuple[int, dict[str, Any]]]:
    if len(codes) < 2:
        raise ValueError("分层先验至少选择两个测年")
    if len(set(codes)) != len(codes):
        raise ValueError("分层样本不能重复")
    samples = [fetch_sample(conn, code) for code in codes]
    missing = [s["code"] for s in samples if s["layer"] is None]
    if missing:
        raise ValueError("以下样本缺少 layer，无法使用分层先验：" + ", ".join(missing))
    ordered = sorted(samples, key=lambda s: (s["layer"], s["code"]))
    layers = [s["layer"] for s in ordered]
    if len(set(layers)) != len(layers):
        raise ValueError("分层先验要求每个样本 layer 唯一（不处理同层并列）")
    curve = load_curve(conn, curve_version)
    results = cal.build_stratified_results(curve, ordered, direction)
    ordered_codes = [s["code"] for s in ordered]
    group = _group_key(curve_version, direction, ordered_codes)
    saved: list[tuple[int, dict[str, Any]]] = []
    for result in results:
        cur = conn.execute(
            "INSERT INTO branches(sample_code,curve_version,direction,prior_kind,group_key)"
            " VALUES (?,?,?,?,?)",
            (result["sample"]["code"], curve_version, direction, "stratified", group),
        )
        branch_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO results(branch_id,result_json,result_hash) VALUES (?,?,?)",
            (branch_id, json.dumps(result, ensure_ascii=False), cal.replay_hash(result)),
        )
        log_action(
            conn,
            "branch",
            f"分层分支 #{branch_id}: {result['sample']['code']} @ {curve_version}",
            branch_id,
        )
        saved.append((branch_id, result))
    return saved


def list_branches(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT b.id,b.sample_code,b.curve_version,b.direction,b.prior_kind,"
        "b.group_key,b.created_at,r.result_hash FROM branches b"
        " JOIN results r ON r.branch_id=b.id ORDER BY b.id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_branch(conn, branch_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT b.*, r.result_json, r.result_hash FROM branches b"
        " JOIN results r ON r.branch_id=b.id WHERE b.id=?",
        (branch_id,),
    ).fetchone()
    if row is None:
        raise LookupError(str(branch_id))
    data = dict(row)
    data["result"] = json.loads(data.pop("result_json"))
    return data


def replay_branch(conn, branch_id: int) -> dict[str, Any]:
    stored = get_branch(conn, branch_id)
    result = stored["result"]
    curve = load_curve(conn, stored["curve_version"])
    sample = fetch_sample(conn, result["sample"]["code"])
    if stored["prior_kind"] == "uniform":
        replayed = cal.build_uniform_result(
            curve, sample, stored["direction"]
        )
    else:
        replayed = _replay_stratified(conn, stored, curve, sample)
    new_hash = cal.replay_hash(replayed)
    old_hash = stored["result_hash"]
    ok = new_hash == old_hash
    log_action(
        conn,
        "replay",
        f"重放分支 #{branch_id}：{'一致' if ok else '不一致'}",
        branch_id,
    )
    return {
        "branch_id": branch_id,
        "match": ok,
        "stored_hash": old_hash,
        "replayed_hash": new_hash,
        "result": replayed,
    }


def _replay_stratified(conn, stored, curve, sample) -> dict[str, Any]:
    row = conn.execute(
        "SELECT sample_code FROM branches WHERE group_key=? ORDER BY id",
        (stored["group_key"],),
    ).fetchall()
    codes = [r["sample_code"] for r in row]
    samples = [fetch_sample(conn, code) for code in codes]
    ordered = sorted(samples, key=lambda s: (s["layer"], s["code"]))
    results = cal.build_stratified_results(
        curve, ordered, stored["direction"]
    )
    for result in results:
        if result["sample"]["code"] == sample["code"]:
            return result
    raise RuntimeError("重放失败：分层组内找不到该样本")


def export_bundle(conn) -> dict[str, Any]:
    samples = [dict(r) for r in conn.execute(
        "SELECT id,code,age,sd,material,reservoir,layer,imported_at FROM samples ORDER BY id"
    ).fetchall()]
    branches = [dict(r) for r in conn.execute(
        "SELECT id,sample_code,curve_version,direction,prior_kind,group_key,created_at"
        " FROM branches ORDER BY id"
    ).fetchall()]
    results = []
    for r in conn.execute(
        "SELECT branch_id,result_json,result_hash FROM results ORDER BY branch_id"
    ).fetchall():
        results.append(
            {
                "branch_id": r["branch_id"],
                "result_hash": r["result_hash"],
                "result": json.loads(r["result_json"]),
            }
        )
    logs = [dict(r) for r in conn.execute(
        "SELECT id,action,detail,branch_id,created_at FROM run_log ORDER BY id"
    ).fetchall()]
    curves = [dict(r) for r in conn.execute(
        "SELECT version,name,summary,source,created_at,knot_hash,knots_json FROM curves"
    ).fetchall()]
    for item in curves:
        item["knots"] = json.loads(item.pop("knots_json"))
    return {
        "export_schema": "carbon-branch-path/export/1",
        "curves": curves,
        "samples": samples,
        "branches": branches,
        "results": results,
        "run_log": logs,
    }


def import_bundle(conn, bundle: dict[str, Any]) -> dict[str, int]:
    if bundle.get("export_schema") != "carbon-branch-path/export/1":
        raise ValueError("导出文件格式或版本不受支持")
    if conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] > 0:
        raise RuntimeError("工作区非空：请先清空数据库再导入运行记录")
    known = {r["version"]: r["knot_hash"] for r in conn.execute(
        "SELECT version,knot_hash FROM curves"
    ).fetchall()}
    for curve in bundle.get("curves", []):
        if curve["version"] not in known:
            raise ValueError(f"导出引用未知曲线版本：{curve['version']}")
        if curve["knot_hash"] != known[curve["version"]]:
            raise ValueError(f"曲线 {curve['version']} 节点哈希与本地版本不一致，禁止合并")

    counts = {"samples": 0, "branches": 0, "results": 0, "logs": 0}
    for s in bundle.get("samples", []):
        conn.execute(
            "INSERT INTO samples(id,code,age,sd,material,reservoir,layer,imported_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (s["id"], s["code"], s["age"], s["sd"], s["material"],
             s["reservoir"], s["layer"], s["imported_at"]),
        )
        counts["samples"] += 1
    for b in bundle.get("branches", []):
        if b["curve_version"] not in known:
            raise ValueError(f"分支 #{b['id']} 引用未知曲线版本，禁止跨版本合并")
        conn.execute(
            "INSERT INTO branches(id,sample_code,curve_version,direction,prior_kind,group_key,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (b["id"], b["sample_code"], b["curve_version"], b["direction"],
             b["prior_kind"], b["group_key"], b["created_at"]),
        )
        counts["branches"] += 1
    for r in bundle.get("results", []):
        result = r["result"]
        actual_hash = cal.replay_hash(result)
        if actual_hash != r["result_hash"]:
            raise ValueError(f"分支结果 #{r['branch_id']} 哈希校验失败")
        conn.execute(
            "INSERT INTO results(branch_id,result_json,result_hash) VALUES (?,?,?)",
            (r["branch_id"], json.dumps(result, ensure_ascii=False), r["result_hash"]),
        )
        counts["results"] += 1
    for entry in bundle.get("run_log", []):
        conn.execute(
            "INSERT INTO run_log(id,action,detail,branch_id,created_at)"
            " VALUES (?,?,?,?,?)",
            (entry["id"], entry["action"], entry["detail"],
             entry["branch_id"], entry["created_at"]),
        )
        counts["logs"] += 1
    log_action(conn, "import_bundle", f"导入运行记录：{counts}")
    return counts
