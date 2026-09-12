"""FastAPI 应用：预演、创建、复核、签发、修订分支、差异、标色 SVG。"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi import HTTPException

from .clauses import clause_catalog
from .engine import evaluate
from .schemas import (
    DiffReport,
    PackageSummary,
    ReviewPack,
    SignRequest,
    IssueRequest,
    SubmissionPayload,
)
from .storage import (
    ConflictError,
    NotFoundError,
    Store,
    diff_versions,
)
from .svg import render_weld_svg

DB_PATH = os.environ.get("WELD_DB_PATH", os.path.join(os.getcwd(), "data", "weld_release.db"))

app = FastAPI(
    title="承压管道焊口合规放行 API",
    version="1.0.0",
    description=(
        "按施焊时点匹配 WPS/PQR 与焊工资格，核算检验批抽检/扩检覆盖率，"
        "并沿缺陷周向位置串接返修、复检与扩检；不合格焊口保持 hold 并列条款。"
    ),
)
store: Store | None = None


@app.on_event("startup")
def _startup() -> None:
    global store
    if store is None:
        store = Store(DB_PATH)


def _store() -> Store:
    if store is None:  # 测试中未触发 startup 时兜底
        store = Store(DB_PATH)
    return store


@app.exception_handler(NotFoundError)
async def _not_found(_: Request, exc: NotFoundError):
    return JSONResponse(status_code=404, content={"error": str(exc)})


@app.exception_handler(ConflictError)
async def _conflict(_: Request, exc: ConflictError):
    return JSONResponse(status_code=409, content={"error": str(exc)})


# ---------------------------------------------------------------- 组装审查包


def _pack(rec: dict) -> dict:
    result = rec["result"]
    return {
        "package_id": rec["package_id"],
        "version": rec["version"],
        "parent_version": rec.get("parent_version"),
        "status": rec["status"],
        "package_ref": rec["package_ref"],
        "submitted_by": rec["snapshot"].get("submitted_by"),
        "decision": result["decision"],
        "snapshot_sha256": rec["snapshot_sha256"],
        "frozen": rec["status"] in ("frozen", "issued"),
        "reviewer": rec.get("reviewer"),
        "reviewed_at": rec.get("reviewed_at"),
        "issuer": rec.get("issuer"),
        "issued_at": rec.get("issued_at"),
        "evaluated_at": result.get("evaluated_at"),
        "stats": result["stats"],
        "lot_summaries": result["lot_summaries"],
        "welds": result["welds"],
        "findings": result["findings"],
        "clauses_triggered": result["clauses_triggered"],
    }


def _attach_links(pid: str, pack: dict) -> None:
    for w in pack["welds"]:
        w["svg_url"] = f"/packages/{pid}/welds/{w['weld_no']}/svg?version={pack['version']}"


# ---------------------------------------------------------------- 健康与条款


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "db": DB_PATH, "time": datetime.now(timezone.utc)}


@app.get("/clauses", tags=["meta"])
def clauses() -> dict:
    """条款目录：所有可能触发的 hold/warning 编号、级别与依据。"""
    return {"clauses": clause_catalog()}


# ---------------------------------------------------------------- 预演


def _evaluate_payload(payload: SubmissionPayload) -> tuple[dict, dict]:
    payload_json = payload.model_dump(mode="json")
    result = evaluate(payload)
    result["evaluated_at"] = result["evaluated_at"].isoformat()
    return payload_json, result


@app.post("/preview", response_model=ReviewPack, tags=["review"],
          summary="合规预演（不落库）")
def preview(payload: SubmissionPayload) -> dict:
    """按当前载荷即时核算，返回完整审查包；不产生持久版本，不可签发。"""
    payload_json, result = _evaluate_payload(payload)
    pack = {
        "package_id": "(preview)",
        "version": 0,
        "package_ref": payload_json.get("package_ref") or "(preview)",
        "submitted_by": payload_json.get("submitted_by"),
        "decision": result["decision"],
        "snapshot_sha256": "0" * 64,
        "evaluated_at": result["evaluated_at"],
        **{k: result[k] for k in
           ("stats", "lot_summaries", "welds", "findings", "clauses_triggered")},
    }
    return pack


@app.post("/preview/welds/{weld_no}/svg", tags=["review"],
          summary="预演单焊口标色 SVG（不落库）")
def preview_svg(weld_no: str, payload: SubmissionPayload) -> Response:
    result = evaluate(payload)
    for w in result["welds"]:
        if w["weld_no"] == weld_no:
            return Response(content=render_weld_svg(w), media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail=f"焊口不存在: {weld_no}")


# ---------------------------------------------------------------- 包生命周期


@app.post("/packages", response_model=ReviewPack, tags=["packages"],
          summary="创建审查包 v1（draft），冻结输入快照")
def create_package(payload: SubmissionPayload) -> dict:
    payload_json, result = _evaluate_payload(payload)
    rec = _store().create_package(payload_json, result, payload.submitted_by)
    pack = _pack(rec)
    _attach_links(rec["package_id"], pack)
    return pack


@app.get("/packages", response_model=list[PackageSummary], tags=["packages"],
         summary="列出审查包")
def list_packages() -> list[dict]:
    return _store().list_packages()


@app.get("/packages/{package_id}", response_model=ReviewPack, tags=["packages"],
         summary="读取审查包（默认当前版本，可 ?version=N 取旧版分支）")
def get_package(package_id: str, version: int | None = None) -> dict:
    pack = _pack(_store().get_version(package_id, version))
    _attach_links(package_id, pack)
    return pack


@app.post("/packages/{package_id}/review", response_model=ReviewPack,
          tags=["packages"], summary="复核签字：冻结当前 draft 版本快照")
def review_package(package_id: str, body: SignRequest) -> dict:
    rec = _store().review(package_id, body.reviewer, body.comment)
    pack = _pack(rec)
    _attach_links(package_id, pack)
    return pack


@app.post("/packages/{package_id}/issue", response_model=ReviewPack,
          tags=["packages"], summary="签发：仅已冻结且整包 release 可签发")
def issue_package(package_id: str, body: IssueRequest) -> dict:
    rec = _store().issue(package_id, body.issuer, body.comment)
    pack = _pack(rec)
    _attach_links(package_id, pack)
    return pack


@app.post("/packages/{package_id}/revisions", response_model=ReviewPack,
          tags=["packages"], summary="修订：从冻结/签发旧版拉出新版分支")
def revise_package(package_id: str, payload: SubmissionPayload) -> dict:
    """纠错从旧版分支：父版保留冻结状态，新载荷作为 draft 新版本重新核算。"""
    payload_json, result = _evaluate_payload(payload)
    rec = _store().revise(package_id, payload_json, result)
    pack = _pack(rec)
    _attach_links(package_id, pack)
    return pack


@app.get("/packages/{package_id}/versions", tags=["packages"],
         summary="列出版本树（含父版本、状态、签字、签发信息）")
def list_versions(package_id: str) -> dict:
    return {"package_id": package_id, "versions": _store().list_versions(package_id)}


@app.get("/packages/{package_id}/diff", response_model=DiffReport,
         tags=["packages"], summary="两个版本输入快照的结构化差异")
def diff_package(package_id: str, from_version: int = 1,
                 to_version: int | None = None) -> dict:
    old = _store().get_version(package_id, from_version)
    new = _store().get_version(package_id, to_version)
    return diff_versions(old, new)


@app.get("/packages/{package_id}/welds/{weld_no}/svg", tags=["packages"],
         summary="单焊口标色 SVG 焊口图")
def weld_svg(package_id: str, weld_no: str, version: int | None = None) -> Response:
    rec = _store().get_version(package_id, version)
    for w in rec["result"]["welds"]:
        if w["weld_no"] == weld_no:
            return Response(content=render_weld_svg(w), media_type="image/svg+xml")
    raise NotFoundError(f"焊口不存在: {weld_no}（{package_id}）")
