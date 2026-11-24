"""归集证据接口：证据导出、断点续传下载、物理迁移/恢复与交接验件。

路由层只做参数编排与 HTTP 表达，全部业务判断在 app.evidence 领域层。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.evidence.service import EvidenceService, get_evidence_service
from app.evidence.store import (
    DigestMismatchError,
    EvidenceStorageError,
    ObjectNotFoundError,
)
from app.evidence.transfer import TransferInterrupted

router = APIRouter(prefix="/api/evidence", tags=["归集证据"])


def svc() -> EvidenceService:
    return get_evidence_service()


def _error(exc: Exception, status: int):
    from fastapi import HTTPException
    raise HTTPException(status_code=status, detail=str(exc))


# ── 请求契约 ──────────────────────────────────────────────────────
class CollectPayload(BaseModel):
    run_id: str
    file_name: str
    content_b64: str = Field(default="", description="原件字节的 base64 编码")
    media_type: str = "application/octet-stream"
    declared_digest: str | None = None


class CorrectPayload(BaseModel):
    run_id: str
    file_name: str
    content_b64: str
    media_type: str = "application/octet-stream"


class SourcePayload(BaseModel):
    source_id: str


class MigratePayload(BaseModel):
    source_id: str | None = None
    digest: str | None = None
    target_volume: str


class RestorePayload(BaseModel):
    source_id: str
    content_b64: str


class DownloadPayload(BaseModel):
    evidence_csv: str = Field(description="证据件 CSV 全文（含表头）")
    signer: str
    byte_budget: int | None = None


class SessionRunPayload(BaseModel):
    byte_budget: int | None = None


class VerifyPayload(BaseModel):
    evidence_csv: str
    restrict_run: str | None = None


def _entry_dict(entry) -> dict[str, Any]:
    return entry.to_dict()


# ── 归集与更正 ────────────────────────────────────────────────────
@router.get("/runs")
def list_runs() -> dict[str, Any]:
    service = svc()
    return {"runs": service.list_runs(),
            "entries": [e.to_dict() for e in service.list_entries()]}


@router.post("/collect")
def collect(payload: CollectPayload) -> dict[str, Any]:
    import base64
    try:
        data = base64.b64decode(payload.content_b64) if payload.content_b64 else b""
        entry = svc().collect(
            payload.run_id, payload.file_name, data,
            media_type=payload.media_type, declared_digest=payload.declared_digest)
    except DigestMismatchError as exc:
        _error(exc, 422)
    except EvidenceStorageError as exc:
        _error(exc, 400)
    return {"ok": True, "entry": _entry_dict(entry)}


@router.post("/correct")
def report_correction(payload: CorrectPayload) -> dict[str, Any]:
    import base64
    try:
        entry = svc().report_correction(
            payload.run_id, payload.file_name,
            base64.b64decode(payload.content_b64), media_type=payload.media_type)
    except ObjectNotFoundError as exc:
        _error(exc, 404)
    except EvidenceStorageError as exc:
        _error(exc, 400)
    return {"ok": True, "entry": _entry_dict(entry)}


# ── 证据导出（来源编号 + 校验值） ─────────────────────────────────
@router.get("/export/{run_id}")
def export_evidence(run_id: str, include_superseded: bool = True) -> StreamingResponse:
    try:
        bundle = svc().export_evidence(
            run_id, include_superseded=include_superseded)
    except ObjectNotFoundError as exc:
        _error(exc, 404)
    filename = f"evidence_{run_id}.csv"
    return StreamingResponse(
        iter([bundle.csv_text]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── 物理路径迁移 / 丢失 / 恢复 ────────────────────────────────────
@router.post("/migrate")
def migrate(payload: MigratePayload) -> dict[str, Any]:
    service = svc()
    try:
        if payload.source_id:
            location = service.migrate_entry(payload.source_id, payload.target_volume)
        elif payload.digest:
            location = service.migrate_object(payload.digest, payload.target_volume)
        else:
            _error(EvidenceStorageError("必须提供 source_id 或 digest"), 400)
    except ObjectNotFoundError as exc:
        _error(exc, 404)
    except EvidenceStorageError as exc:
        _error(exc, 400)
    return {"ok": True, "location": location.to_dict()}


@router.post("/mark-lost")
def mark_lost(payload: SourcePayload) -> dict[str, Any]:
    try:
        svc().mark_lost(payload.source_id)
    except ObjectNotFoundError as exc:
        _error(exc, 404)
    return {"ok": True, "source_id": payload.source_id}


@router.post("/restore")
def restore(payload: RestorePayload) -> dict[str, Any]:
    import base64
    try:
        location = svc().restore_source(
            payload.source_id, base64.b64decode(payload.content_b64))
    except ObjectNotFoundError as exc:
        _error(exc, 404)
    except DigestMismatchError as exc:
        _error(exc, 422)
    return {"ok": True, "source_id": payload.source_id,
            "location": location.to_dict()}


@router.get("/state/{source_id}")
def storage_state(source_id: str) -> dict[str, Any]:
    try:
        state = svc().storage_state(source_id)
    except ObjectNotFoundError as exc:
        _error(exc, 404)
    return {"source_id": source_id, "state": state}


# ── 断点续传下载 ──────────────────────────────────────────────────
@router.post("/downloads")
def start_download(payload: DownloadPayload) -> dict[str, Any]:
    session = svc().start_download(payload.evidence_csv, payload.signer)
    # 创建后立即按请求预算推进一次；预算耗尽返回可恢复的中断摘要
    return {"ok": True, "session_id": session.session_id,
            "summary": _safe_run(session, payload.byte_budget)}


@router.post("/downloads/{session_id}/run")
def run_download(session_id: str, payload: SessionRunPayload) -> dict[str, Any]:
    try:
        session = svc().open_download(session_id)
    except ObjectNotFoundError as exc:
        _error(exc, 404)
    return {"ok": True, "summary": _safe_run(session, payload.byte_budget)}


def _safe_run(session, byte_budget: int | None) -> dict[str, Any]:
    try:
        return session.run(byte_budget=byte_budget)
    except TransferInterrupted as exc:
        summary = session.summary()
        summary["interrupted"] = True
        summary["message"] = str(exc)
        return summary


@router.get("/downloads/{session_id}")
def download_status(session_id: str) -> dict[str, Any]:
    try:
        session = svc().open_download(session_id)
    except ObjectNotFoundError as exc:
        _error(exc, 404)
    return session.summary()


# ── 交接验件 ──────────────────────────────────────────────────────
@router.post("/verify")
def verify(payload: VerifyPayload) -> dict[str, Any]:
    report = svc().verify(payload.evidence_csv, restrict_run=payload.restrict_run)
    return report.to_dict()
