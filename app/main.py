from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

from . import db, services
from .calibration import GRID_START, GRID_STEP, GRID_STOP, PRIOR_VERSIONS


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db(seed=True)
    yield


app = FastAPI(title="碳纪枝径", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class ImportPayload(BaseModel):
    records: list[dict[str, Any]] | None = None
    csv_text: str | None = None
    source: str = "api"


class BranchPayload(BaseModel):
    curve_id: str
    prior_key: str = "uniform-v1"
    direction: str = "BP_OLDER_POSITIVE"
    coverages: list[float] = Field(default_factory=services.default_coverages)


@app.get("/", response_class=PlainTextResponse)
def index() -> str:
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "碳纪枝径"}


@app.get("/api/curves")
def curves() -> list[dict[str, Any]]:
    return db.list_curves()


@app.get("/api/priors")
def priors() -> dict[str, Any]:
    return {
        "fixed_grid": {"start_bp": GRID_START, "stop_bp": GRID_STOP, "step_years": GRID_STEP},
        "priors": list(PRIOR_VERSIONS.values()),
    }


@app.post("/api/import/fixture")
def import_fixture() -> dict[str, Any]:
    text = (db.ROOT / "fixtures" / "imports" / "fixed-samples.csv").read_text(encoding="utf-8")
    rows = db.parse_csv(text)
    result = db.import_rows(rows, "fixture:fixed-samples.csv")
    db.record_run("import_fixture", "samples", {}, "ok", result)
    return result


@app.post("/api/import")
def import_data(payload: ImportPayload) -> dict[str, Any]:
    try:
        if payload.csv_text is not None:
            rows = db.parse_csv(payload.csv_text)
        elif payload.records is not None:
            rows = payload.records
        else:
            raise ValueError("请提供 csv_text 或 records")
        result = db.import_rows(rows, payload.source)
    except (ValueError, KeyError, TypeError) as exc:
        db.record_run("import", "samples", payload.model_dump(), "error", str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.record_run("import", "samples", {"source": payload.source, "count": result["count"]}, "ok", result)
    return result


@app.get("/api/samples")
def samples() -> list[dict[str, Any]]:
    return db.list_samples()


@app.get("/api/samples/{sample_id}")
def sample(sample_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        return dict(db.get_sample(conn, sample_id))


@app.post("/api/samples/{sample_id}/branches")
def create_branch(sample_id: int, payload: BranchPayload) -> dict[str, Any]:
    try:
        return services.create_or_replay_branch(
            sample_id,
            payload.curve_id,
            payload.prior_key,
            payload.direction,
            payload.coverages,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/branches")
def branches(sample_id: int | None = Query(default=None)) -> dict[str, Any]:
    if sample_id is None:
        return services.branch_collection()
    return {"merge_policy": "different_curve_versions_never_merged", "weighted_mean_replacement": False, "branches": services.list_branches(sample_id)}


@app.get("/api/branches/{branch_id}")
def branch(branch_id: int) -> dict[str, Any]:
    try:
        return services.get_branch(branch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/branches/{branch_id}/replay")
def replay_branch(branch_id: int) -> dict[str, Any]:
    try:
        current = services.get_branch(branch_id)
        result = services.create_or_replay_branch(
            current["sample_id"],
            current["curve"]["curve_id"],
            current["prior_key"],
            current["direction"]["key"],
            current["coverages"],
        )
        db.record_run("replay", f"branch:{branch_id}", {"target_branch_id": branch_id}, "ok", {"returned_branch_id": result["branch"]["branch_id"], "replayed": result["replayed"]})
        result["source_branch_id"] = branch_id
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/runs")
def runs(limit: int = Query(default=200, ge=1, le=1000)) -> list[dict[str, Any]]:
    return db.list_runs(limit)


@app.get("/api/runs/export")
def export_runs() -> PlainTextResponse:
    rows = db.list_runs(1000)
    text = json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True)
    return PlainTextResponse(text, media_type="application/json; charset=utf-8", headers={"Content-Disposition": "attachment; filename=carbon-branch-runs.json"})


@app.post("/api/admin/clear")
def clear_database() -> dict[str, Any]:
    result = db.clear_and_reseed()
    db.record_run("admin_clear", "database", {}, "ok", result)
    return {"cleared": True, "curves_reseeded": result}
