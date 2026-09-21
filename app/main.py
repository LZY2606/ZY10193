from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import services
from .db import clear_workspace, connect, init_db, list_curves, log_action

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="碳纪枝径", version="1.0.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (
        Path(__file__).resolve().parent.parent / "static" / "index.html"
    ).read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "碳纪枝径"}


@app.get("/api/curves")
def curves() -> list[dict[str, Any]]:
    with connect() as conn:
        return list_curves(conn)


@app.get("/api/samples")
def samples() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM samples ORDER BY id"
        ).fetchall()]


@app.post("/api/import")
async def import_samples(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")
        parsed = services.parse_samples(content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"导入失败：{exc}") from exc
    with connect() as conn:
        counts = services.upsert_samples(conn, parsed)
    return {"imported": counts["inserted"], "updated": counts["updated"]}


class UniformBranchIn(BaseModel):
    code: str
    curve_version: str
    direction: str = "oldward_positive"


@app.post("/api/branches")
def create_branch(payload: UniformBranchIn) -> dict[str, Any]:
    with connect() as conn:
        try:
            branch_id, result = services.create_uniform_branch(
                conn,
                code=payload.code,
                curve_version=payload.curve_version,
                direction=payload.direction,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=f"未找到：{exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"branch_id": branch_id, "result": result}


class StratifiedBranchIn(BaseModel):
    codes: list[str] = Field(min_length=2)
    curve_version: str
    direction: str = "oldward_positive"


@app.post("/api/branches/stratified")
def create_stratified(payload: StratifiedBranchIn) -> dict[str, Any]:
    with connect() as conn:
        try:
            saved = services.create_stratified_branches(
                conn,
                codes=payload.codes,
                curve_version=payload.curve_version,
                direction=payload.direction,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=f"未找到：{exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"branches": [{"branch_id": bid, "result": r} for bid, r in saved]}


@app.get("/api/branches")
def branches() -> list[dict[str, Any]]:
    with connect() as conn:
        return services.list_branches(conn)


@app.get("/api/branches/{branch_id}")
def branch_detail(branch_id: int) -> dict[str, Any]:
    with connect() as conn:
        try:
            return services.get_branch(conn, branch_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=f"分支不存在：{exc}") from exc


@app.post("/api/branches/{branch_id}/replay")
def replay(branch_id: int) -> dict[str, Any]:
    with connect() as conn:
        try:
            return services.replay_branch(conn, branch_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=f"分支不存在：{exc}") from exc


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM run_log ORDER BY id DESC LIMIT 200"
        ).fetchall()]


@app.get("/api/export")
def export_runs() -> JSONResponse:
    with connect() as conn:
        bundle = services.export_bundle(conn)
    return JSONResponse(
        bundle,
        headers={
            "Content-Disposition": (
                'attachment; filename="carbon_branch_runs.json"'
            )
        },
    )


@app.post("/api/import-runs")
async def import_runs(file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        bundle = json.loads((await file.read()).decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败：{exc}") from exc
    with connect() as conn:
        try:
            counts = services.import_bundle(conn, bundle)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"imported": counts}


@app.post("/api/reset")
def reset() -> dict[str, str]:
    with connect() as conn:
        clear_workspace(conn)
        log_action(conn, "reset", "清空样本、分支、结果与运行记录（保留版本化曲线）")
    return {"status": "reset"}
