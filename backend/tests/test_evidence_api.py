"""归集证据 HTTP 路由冒烟测试：不启动网络服务，直接调用路由处理函数。

通过 monkeypatch 把路由使用的单例服务替换为临时目录实例，覆盖：
归集/更正、证据导出、迁移/丢失/恢复、下载会话与交接验件，以及 404/422 错误语义。
"""
from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from app import routers
from app.evidence.service import EvidenceService
from app.routers import evidence as evidence_router


@pytest.fixture()
def api(tmp_path, monkeypatch):
    service = EvidenceService(tmp_path / "evidence", volumes=("v1", "v2"))
    monkeypatch.setattr(evidence_router, "get_evidence_service", lambda: service)
    return service


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _csv_text(response) -> str:
    """当前 Starlette 会把 StreamingResponse 的同步迭代器包成异步生成器。"""
    import asyncio

    async def collect():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return "".join(chunks)

    return asyncio.run(collect())


def test_evidence_routes_registered():
    paths = {route.path for route in routers.evidence.router.routes}
    expected = {
        "/api/evidence/collect", "/api/evidence/correct",
        "/api/evidence/export/{run_id}", "/api/evidence/migrate",
        "/api/evidence/mark-lost", "/api/evidence/restore",
        "/api/evidence/downloads", "/api/evidence/verify",
    }
    assert expected.issubset(paths)


def test_collect_export_verify_flow(api):
    payload = evidence_router.CollectPayload(
        run_id="RUN-A", file_name="inventory.csv", content_b64=_b64(b"hello\n"))
    created = evidence_router.collect(payload)
    assert created["ok"] is True
    source_id = created["entry"]["source_id"]
    assert source_id == "RUN-A#0001"

    response = evidence_router.export_evidence("RUN-A")
    csv_text = _csv_text(response)
    assert source_id in csv_text and "sha256" in csv_text

    report = evidence_router.verify(evidence_router.VerifyPayload(evidence_csv=csv_text))
    assert report["ok"] == 1 and report["missing"] == 0


def test_correction_and_migration_endpoints(api):
    evidence_router.collect(evidence_router.CollectPayload(
        run_id="RUN-A", file_name="f.csv", content_b64=_b64(b"v1")))
    corrected = evidence_router.report_correction(evidence_router.CorrectPayload(
        run_id="RUN-A", file_name="f.csv", content_b64=_b64(b"v2-corrected")))
    assert corrected["entry"]["supersedes"] == "RUN-A#0001"

    moved = evidence_router.migrate(evidence_router.MigratePayload(
        source_id="RUN-A#0001", target_volume="v2"))
    assert moved["location"]["volume"] == "v2"

    with pytest.raises(HTTPException) as exc:
        evidence_router.migrate(evidence_router.MigratePayload(
            source_id="RUN-X#9999", target_volume="v2"))
    assert exc.value.status_code == 404


def test_lost_restore_and_wrong_bytes_rejected(api):
    evidence_router.collect(evidence_router.CollectPayload(
        run_id="RUN-A", file_name="f.csv", content_b64=_b64(b"original-bytes")))
    evidence_router.mark_lost(evidence_router.SourcePayload(source_id="RUN-A#0001"))
    assert evidence_router.storage_state("RUN-A#0001")["state"] == "lost"

    with pytest.raises(HTTPException) as exc:
        evidence_router.restore(evidence_router.RestorePayload(
            source_id="RUN-A#0001", content_b64=_b64(b"wrong-bytes")))
    assert exc.value.status_code == 422

    evidence_router.restore(evidence_router.RestorePayload(
        source_id="RUN-A#0001", content_b64=_b64(b"original-bytes")))
    assert evidence_router.storage_state("RUN-A#0001")["state"] == "available"


def test_download_session_interrupt_and_resume(api):
    evidence_router.collect(evidence_router.CollectPayload(
        run_id="RUN-A", file_name="f.csv", content_b64=_b64(b"x" * 50)))
    csv_text = _csv_text(evidence_router.export_evidence("RUN-A"))

    started = evidence_router.start_download(evidence_router.DownloadPayload(
        evidence_csv=csv_text, signer="签收人", byte_budget=10))
    assert started["summary"].get("interrupted") is True
    session_id = started["session_id"]

    resumed = evidence_router.run_download(
        session_id, evidence_router.SessionRunPayload())
    assert resumed["summary"]["complete"] is True
    assert evidence_router.download_status(session_id)["done"] == 1


def test_verify_reports_missing_with_recoverable_source(api):
    evidence_router.collect(evidence_router.CollectPayload(
        run_id="RUN-A", file_name="f.csv", content_b64=_b64(b"abc")))
    csv_text = _csv_text(evidence_router.export_evidence("RUN-A"))
    evidence_router.mark_lost(evidence_router.SourcePayload(source_id="RUN-A#0001"))

    report = evidence_router.verify(evidence_router.VerifyPayload(evidence_csv=csv_text))
    assert report["missing"] == 1
    assert report["missing_source_ids"] == ["RUN-A#0001"]
