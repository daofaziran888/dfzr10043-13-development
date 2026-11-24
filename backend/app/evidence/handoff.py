"""交接验件：接收侧读入证据件，按清单来源与摘要产生文件核对结果。

核对完全以证据行的 ``source_id`` 为主键（而不是文件名），因此：
- 执行乙的同名件不会替代执行甲：不同 run/source_id 各自定位各自的原件；
- 旧索引重新读取归集件时，凭 source_id 仍能定位已被更正但保留的原件；
- 重复核验是集合语义：同一 source_id 无论出现/核验多少次，缺件只计一条；
- 物理原件丢失报 MISSING 并给出待恢复来源编号；摘要不符报 MISMATCH 拒收。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.evidence.export import EvidenceRow, parse_evidence_csv
from app.evidence.registry import CollectionRegistry
from app.evidence.store import (
    ContentAddressedStore,
    DigestMismatchError,
    ObjectNotFoundError,
)

OK = "ok"
MISSING = "missing"
MISMATCH = "mismatch"


@dataclass
class CheckResult:
    """单个来源编号的核对结果。"""

    source_id: str
    run_id: str
    file_name: str
    expected_digest: str
    actual_digest: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id, "run_id": self.run_id,
            "file_name": self.file_name, "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest, "status": self.status,
            "detail": self.detail,
        }


@dataclass
class VerificationReport:
    """整份交接包的核对汇总，缺件/不符按来源编号去重。"""

    results: list[CheckResult] = field(default_factory=list)

    def by_status(self, status: str) -> list[CheckResult]:
        return [result for result in self.results if result.status == status]

    @property
    def ok_rows(self) -> list[CheckResult]:
        return self.by_status(OK)

    @property
    def missing_rows(self) -> list[CheckResult]:
        return self.by_status(MISSING)

    @property
    def mismatch_rows(self) -> list[CheckResult]:
        return self.by_status(MISMATCH)

    def to_dict(self) -> dict[str, object]:
        return {
            "total": len(self.results),
            "ok": len(self.ok_rows),
            "missing": len(self.missing_rows),
            "mismatch": len(self.mismatch_rows),
            "missing_source_ids": [row.source_id for row in self.missing_rows],
            "mismatch_source_ids": [row.source_id for row in self.mismatch_rows],
            "results": [row.to_dict() for row in self.results],
        }


def locate_original(registry: CollectionRegistry, source_id: str):
    """接收者用旧索引重新读取时，凭来源编号定位对应原件登记条目。"""
    return registry.get_entry(source_id)


def verify_handoff(
    evidence_csv: str,
    registry: CollectionRegistry,
    store: ContentAddressedStore,
    *,
    restrict_run: str | None = None,
) -> VerificationReport:
    """读入交接包证据件，逐行按来源定位原件并复核 sha256。

    重复 source_id 只产生一条结果（集合语义），重复核验不会把同一件计成两条缺件。
    """
    rows = parse_evidence_csv(evidence_csv)
    if restrict_run is not None:
        rows = [row for row in rows if row.run_id == restrict_run]

    report = VerificationReport()
    seen: set[str] = set()
    for row in rows:
        if row.source_id in seen:
            continue
        seen.add(row.source_id)
        report.results.append(_check_row(row, registry, store))
    return report


def _check_row(row: EvidenceRow, registry: CollectionRegistry,
               store: ContentAddressedStore) -> CheckResult:
    # 1) 来源编号能否在登记台账定位（旧索引里的 superseded 条目同样可定位）
    try:
        entry = registry.get_entry(row.source_id)
    except ObjectNotFoundError as exc:
        return CheckResult(
            row.source_id, row.run_id, row.file_name, row.sha256, "",
            MISSING, f"来源编号未登记，尚须恢复：{exc}")

    # 2) 登记摘要必须与证据一致，否则说明索引与归集内容脱节
    if entry.digest != row.sha256:
        return CheckResult(
            row.source_id, row.run_id, row.file_name, row.sha256, entry.digest,
            MISMATCH, "登记摘要与证据件不一致")

    # 3) 物理原件是否可读（路径迁走只改 catalog，真正丢失才报缺件）
    try:
        handle = store.open_blob(entry.digest)
    except ObjectNotFoundError as exc:
        return CheckResult(
            row.source_id, row.run_id, row.file_name, row.sha256, "",
            MISSING, f"原件物理丢失，尚须恢复原归集编号 {row.source_id}：{exc}")

    # 4) 实读字节复核 sha256
    digest = hashlib.sha256()
    with handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != row.sha256:
        return CheckResult(
            row.source_id, row.run_id, row.file_name, row.sha256, actual,
            MISMATCH, "实读原件 sha256 与证据不符")
    if entry.size != row.size:
        return CheckResult(
            row.source_id, row.run_id, row.file_name, row.sha256, actual,
            MISMATCH, f"字节数 {entry.size} 与证据 {row.size} 不符")
    return CheckResult(
        row.source_id, row.run_id, row.file_name, row.sha256, actual, OK, "核对通过")
