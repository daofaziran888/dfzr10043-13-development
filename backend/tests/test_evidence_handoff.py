"""归集证据与交接验件：完整叙述场景的确定性测试。

覆盖题目全部要求：
1. 证据导出来源编号 + 校验值，行可指出内容来自哪次归集；
2. 同名更正只追加，旧版本字节保留，旧任务引用不失效；
3. 导出过程中正赶上物理路径迁移，证据仍可验；
4. 下载中断后签收人凭原证据续验，更正件不能在途替换原内容；
5. 重复核验不把同一件计成两条缺件；
6. 交接包读入证据件按来源与摘要核对，报告缺件/校验不符；
7. 旧索引重新读取仍能定位原件；
8. 执行乙同名件不得替代执行甲；
9. 原件丢失给出待恢复来源编号，修复后旧索引恢复可验，错字节拒绝恢复。
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from app.evidence.export import (
    EVIDENCE_HEADER,
    export_run_evidence,
    parse_evidence_csv,
)
from app.evidence.handoff import MISMATCH, MISSING, OK, verify_handoff
from app.evidence.registry import ACTIVE, SUPERSEDED
from app.evidence.service import EvidenceService
from app.evidence.store import DigestMismatchError, ObjectNotFoundError
from app.evidence.transfer import (
    BLOCKED_MISSING,
    DONE,
    IN_PROGRESS,
    TransferInterrupted,
)

RUN_JIA = "RUN-JIA"
RUN_YI = "RUN-YI"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def service(tmp_path) -> EvidenceService:
    return EvidenceService(tmp_path / "evidence", volumes=("v1", "v2"))


def _seed_two_runs(service: EvidenceService) -> dict[str, bytes]:
    """执行甲登记 inventory.csv + detail.csv；执行乙另建同名 inventory.csv。"""
    payloads = {
        "jia_inventory": b"id,name\n1,alpha\n",
        "jia_detail": b"detail-bytes-jia\n",
        "yi_inventory": b"id,name\n9,beta-different-content\n",
    }
    service.collect(RUN_JIA, "inventory.csv", payloads["jia_inventory"],
                    media_type="text/csv")
    service.collect(RUN_JIA, "detail.csv", payloads["jia_detail"],
                    media_type="text/csv")
    service.collect(RUN_YI, "inventory.csv", payloads["yi_inventory"],
                    media_type="text/csv")
    return payloads


class TestEvidenceExport:
    def test_export_carries_source_id_and_checksum(self, service):
        _seed_two_runs(service)
        bundle = service.export_evidence(RUN_JIA)
        assert [line.split(",")[0] for line in bundle.csv_text.splitlines()[1:]] == [
            f"{RUN_JIA}#0001", f"{RUN_JIA}#0002"]
        rows = parse_evidence_csv(bundle.csv_text)
        assert {r.source_id for r in rows} == {f"{RUN_JIA}#0001", f"{RUN_JIA}#0002"}
        assert all(len(r.sha256) == 64 for r in rows)
        assert rows[0].run_id == RUN_JIA  # 每行指出内容来自哪次归集
        header = bundle.csv_text.lstrip("﻿").splitlines()[0].split(",")
        assert header == EVIDENCE_HEADER

    def test_export_mid_run_migration_still_verifiable(self, service):
        _seed_two_runs(service)
        migrated: list[str] = []

        def migrate_on_row(entry):
            # 导出过程中正赶上原件存储路径被迁走：迁到 v2 并删除旧物理路径
            location = service.migrate_entry(entry.source_id, "v2")
            assert location.volume == "v2"
            migrated.append(entry.source_id)

        bundle = export_run_evidence(
            service.registry, service.store, RUN_JIA, on_row=migrate_on_row)
        assert len(migrated) == 2
        assert set(bundle.storage_state.values()) == {"available"}
        # catalog 已换定位，交接验件仍按来源编号取到原件
        report = service.verify(bundle.csv_text)
        assert report.to_dict()["missing"] == 0
        assert len(report.ok_rows) == 2


class TestCorrectionPreservesOriginal:
    def test_same_name_correction_appends_and_keeps_old_bytes(self, service):
        payloads = _seed_two_runs(service)
        old_entry = service.registry.get_entry(f"{RUN_JIA}#0001")
        corrected = b"id,name\n1,alpha-corrected\n"

        new_entry = service.report_correction(RUN_JIA, "inventory.csv", corrected)

        assert new_entry.source_id == f"{RUN_JIA}#0003"
        assert new_entry.supersedes == old_entry.source_id
        old = service.registry.get_entry(old_entry.source_id)
        assert old.status == SUPERSEDED
        assert new_entry.status == ACTIVE
        # 旧字节仍可凭旧来源编号取回，未被更正件替换
        with service.registry.open_source(old_entry.source_id) as handle:
            assert handle.read() == payloads["jia_inventory"]
        with service.registry.open_source(new_entry.source_id) as handle:
            assert handle.read() == corrected
        # 证据默认含 superseded，旧任务引用仍可验
        bundle = service.export_evidence(RUN_JIA)
        assert len(bundle.rows) == 3
        active_only = service.export_evidence(RUN_JIA, include_superseded=False)
        assert [r.source_id for r in active_only.rows] == [
            f"{RUN_JIA}#0002", f"{RUN_JIA}#0003"]

    def test_correction_requires_existing_active_file(self, service):
        service.collect(RUN_JIA, "only.csv", b"x")
        with pytest.raises(ObjectNotFoundError):
            service.report_correction(RUN_JIA, "missing.csv", b"y")


class TestResumableDownload:
    def test_interrupted_resume_keeps_original_evidence(self, service):
        payloads = _seed_two_runs(service)
        evidence = service.export_evidence(RUN_JIA).csv_text
        session = service.start_download(evidence, signer="签收人甲")

        with pytest.raises(TransferInterrupted):
            session.run(byte_budget=5)
        assert session.summary()["done"] < 2
        assert any(i.state == IN_PROGRESS for i in session.items)

        # 下载者还没读完归集件时，机器人补报同名更正件
        service.report_correction(RUN_JIA, "inventory.csv", b"corrected-bytes\n")
        # 同时迁走旧物理路径
        old_source = f"{RUN_JIA}#0001"
        service.migrate_entry(old_source, "v2")

        # 用旧索引恢复下载（重新打开会话，先前签收人仍可用原证据）
        resumed = service.open_download(session.session_id)
        assert resumed.signer == "签收人甲"
        summary = resumed.run()  # 不给预算即一次跑完
        assert summary["complete"] is True
        assert summary["done"] == 2

        # 落盘内容必须是旧证据对应的原字节，而非更正件
        inventory_path = resumed.payload_path(old_source)
        assert inventory_path.read_bytes() == payloads["jia_inventory"]
        assert sha256(inventory_path.read_bytes()) == resumed.item(old_source).expected_digest

    def test_blocked_missing_then_restore_then_resume_succeeds(self, service):
        _seed_two_runs(service)
        evidence = service.export_evidence(RUN_JIA).csv_text
        lost_source = f"{RUN_JIA}#0002"
        original = service.registry.get_entry(lost_source)
        original_bytes = service.store.read_bytes(original.digest)
        service.mark_lost(lost_source)

        session = service.start_download(evidence, signer="签收人乙")
        summary = session.run()
        assert summary["blocked_missing"] == 1
        assert summary["missing_source_ids"] == [lost_source]
        assert session.item(lost_source).state == BLOCKED_MISSING
        assert "尚须恢复" in session.item(lost_source).detail or "待恢复" in session.item(
            lost_source).detail

        # 恢复原件后，用同一旧索引重新跑即可成功，不产生新缺件
        service.restore_source(lost_source, original_bytes)
        again = service.open_download(session.session_id).run()
        assert again["complete"] is True
        assert again["blocked_missing"] == 0

    def test_restore_rejects_wrong_bytes(self, service):
        _seed_two_runs(service)
        source = f"{RUN_JIA}#0001"
        service.mark_lost(source)
        with pytest.raises(DigestMismatchError):
            service.restore_source(source, b"totally different bytes")

    def test_rerun_is_idempotent_and_does_not_double_count(self, service):
        _seed_two_runs(service)
        evidence = service.export_evidence(RUN_JIA).csv_text
        session = service.start_download(evidence, signer="签收人丙")
        first = session.run()
        second = session.run()
        third = service.open_download(session.session_id).run()
        for summary in (first, second, third):
            assert summary["total"] == 2
            assert summary["done"] == 2
            assert summary["missing_source_ids"] == []

    def test_tampered_partial_file_is_restarted_not_trusted(self, service):
        _seed_two_runs(service)
        evidence = service.export_evidence(RUN_JIA).csv_text
        session = service.start_download(evidence, signer="签收人丁")
        with pytest.raises(TransferInterrupted):
            session.run(byte_budget=4)
        item = session.item(f"{RUN_JIA}#0001")
        part = session.payload_path(item.source_id).with_name(
            session.payload_path(item.source_id).name + ".part")
        assert part.exists()
        part.write_bytes(b"X" * part.stat().st_size)  # 破坏半截文件

        summary = service.open_download(session.session_id).run()
        assert summary["complete"] is True
        assert summary["blocked_mismatch"] == 0


class TestHandoffVerification:
    def test_full_verification_ok(self, service):
        _seed_two_runs(service)
        report = service.verify(service.export_evidence(RUN_JIA).csv_text)
        assert {r.status for r in report.results} == {OK}
        assert report.to_dict()["ok"] == 2

    def test_missing_and_mismatch_are_reported_distinctly(self, service, tmp_path):
        import dataclasses
        _seed_two_runs(service)
        evidence = service.export_evidence(RUN_JIA).csv_text
        # 一件物理丢失
        service.mark_lost(f"{RUN_JIA}#0002")
        # 一件篡改证据摘要（模拟清单与归集内容脱节）
        rows = parse_evidence_csv(evidence)
        rows[0] = dataclasses.replace(rows[0], sha256="0" * 64)
        tampered = "﻿" + ",".join(EVIDENCE_HEADER) + "\n" + "\n".join(
            ",".join(str(v) for v in row.to_csv_row()) for row in rows) + "\n"

        report = service.verify(tampered)
        statuses = {r.source_id: r.status for r in report.results}
        assert statuses[f"{RUN_JIA}#0001"] == MISMATCH
        assert statuses[f"{RUN_JIA}#0002"] == MISSING
        payload = report.to_dict()
        assert payload["missing_source_ids"] == [f"{RUN_JIA}#0002"]
        assert payload["mismatch_source_ids"] == [f"{RUN_JIA}#0001"]

    def test_repeated_verification_counts_each_source_once(self, service):
        _seed_two_runs(service)
        rows = service.export_evidence(RUN_JIA).rows
        # 证据件中同一来源编号重复出现
        duplicated = "﻿" + ",".join(EVIDENCE_HEADER) + "\n" + "\n".join(
            ",".join(str(v) for v in row.to_csv_row())
            for row in [*rows, *rows, rows[0]])
        report = service.verify(duplicated)
        assert len(report.results) == 2
        assert report.to_dict()["total"] == 2

        service.mark_lost(f"{RUN_JIA}#0001")
        report_again = service.verify(duplicated)
        assert report_again.to_dict()["missing"] == 1  # 不会计成多条缺件

    def test_old_index_locates_superseded_original(self, service):
        payloads = _seed_two_runs(service)
        old_evidence = service.export_evidence(RUN_JIA).csv_text
        service.report_correction(RUN_JIA, "inventory.csv", b"new version\n")

        # 接收者持旧索引重新读取归集件
        report = service.verify(old_evidence)
        assert report.to_dict()["ok"] == 2
        located = service.registry.get_entry(f"{RUN_JIA}#0001")
        with service.registry.open_source(located.source_id) as handle:
            assert handle.read() == payloads["jia_inventory"]

    def test_other_run_same_name_file_cannot_substitute(self, service):
        payloads = _seed_two_runs(service)
        jia_evidence = service.export_evidence(RUN_JIA).csv_text
        # 甲的 inventory.csv 原件丢失；乙有同名件但属于另一次执行
        service.mark_lost(f"{RUN_JIA}#0001")
        report = service.verify(jia_evidence)
        jia_result = next(r for r in report.results if r.source_id == f"{RUN_JIA}#0001")
        assert jia_result.status == MISSING  # 乙的同名件不得替代甲

        # 恢复甲自己的原字节后才通过；乙的字节冒充甲会被拒
        with pytest.raises(DigestMismatchError):
            service.restore_source(f"{RUN_JIA}#0001", payloads["yi_inventory"])
        service.restore_source(f"{RUN_JIA}#0001", payloads["jia_inventory"])
        assert service.verify(jia_evidence).to_dict()["missing"] == 0

    def test_collect_rejects_declared_digest_mismatch(self, service):
        with pytest.raises(DigestMismatchError):
            service.collect(RUN_JIA, "bad.csv", b"abc",
                            declared_digest="f" * 64)


class TestEndToEndNarrative:
    """题面完整故事：暂停读取 → 补报更正 → 迁走旧路径 → 旧索引恢复与验件。"""

    def test_full_narrative(self, service):
        payloads = _seed_two_runs(service)
        old_source = f"{RUN_JIA}#0001"
        detail_source = f"{RUN_JIA}#0002"

        # 1) 取得甲的旧索引（证据件），随后暂停实际读取
        old_index = service.export_evidence(RUN_JIA).csv_text
        session = service.start_download(old_index, signer="交接签收人")
        with pytest.raises(TransferInterrupted):
            session.run(byte_budget=3)
        partial = session.item(old_source)
        assert partial.state == IN_PROGRESS and partial.offset > 0

        # 2) 补报甲的新内容（同名更正件），旧条目不删不改
        corrected = b"id,name\n1,alpha-v2-correction\n"
        new_entry = service.report_correction(RUN_JIA, "inventory.csv", corrected)
        assert service.registry.get_entry(old_source).status == SUPERSEDED

        # 3) 迁走旧物理路径（v1 → v2）
        service.migrate_entry(old_source, "v2")
        service.migrate_entry(detail_source, "v2")
        assert service.storage_state(old_source) == "available"

        # 4) 用旧索引恢复下载并交接验件
        resumed = service.open_download(session.session_id)
        summary = resumed.run()
        assert summary["complete"] is True
        assert resumed.payload_path(old_source).read_bytes() == payloads["jia_inventory"]

        report = verify_handoff(old_index, service.registry, service.store)
        assert report.to_dict()["ok"] == 2 and report.to_dict()["missing"] == 0

        # 5) 原件再丢失：明确指出尚须恢复的原归集编号，乙同名件不能顶替
        service.mark_lost(detail_source)
        broken = service.verify(old_index)
        assert broken.to_dict()["missing_source_ids"] == [detail_source]

        # 6) 修复原件后旧索引又能验证
        service.restore_source(detail_source, payloads["jia_detail"])
        healed = service.verify(old_index)
        assert healed.to_dict()["ok"] == 2 and healed.to_dict()["missing"] == 0

        # 7) 新证据（含更正件）独立可验，甲乙两次执行互不串件
        new_index = service.export_evidence(RUN_JIA)
        assert {r.source_id for r in new_index.rows} == {
            old_source, detail_source, new_entry.source_id}
        yi_bundle = service.export_evidence(RUN_YI)
        assert [r.file_name for r in yi_bundle.rows] == ["inventory.csv"]
        assert yi_bundle.rows[0].sha256 == sha256(payloads["yi_inventory"])
        assert yi_bundle.rows[0].sha256 != new_entry.digest
