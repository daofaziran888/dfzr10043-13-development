"""归集证据子系统：让交接包能按实际归集字节验件，而不只依赖列表 CSV。

能力总览：
- store     内容寻址原件仓：字节按 sha256 保存，物理路径迁移不改来源编号与摘要
- registry  归集登记：按执行登记来源编号；同名更正只追加，原字节永不消失
- export    证据导出：按执行导出来源编号 + 校验值证据件（inventory 证据 CSV）
- transfer  断点续传下载：固定证据快照，中断后签收人凭原证据继续，不被更正件替换
- handoff   交接验件：读入证据件，按来源定位原件并核对摘要，报告缺件/不符
"""
from __future__ import annotations

from app.evidence.export import (
    EVIDENCE_HEADER,
    EvidenceBundle,
    export_run_evidence,
    parse_evidence_csv,
    render_evidence_csv,
)
from app.evidence.handoff import (
    MISMATCH,
    MISSING,
    OK,
    CheckResult,
    VerificationReport,
    verify_handoff,
)
from app.evidence.registry import CollectionEntry, CollectionRegistry
from app.evidence.service import EvidenceService, get_evidence_service
from app.evidence.store import (
    ContentAddressedStore,
    DigestMismatchError,
    EvidenceStorageError,
    ObjectLocation,
    ObjectNotFoundError,
)
from app.evidence.transfer import (
    BLOCKED_MISMATCH,
    BLOCKED_MISSING,
    DONE,
    IN_PROGRESS,
    PENDING,
    DownloadSession,
    TransferInterrupted,
    TransferItem,
)

__all__ = [
    "ContentAddressedStore",
    "ObjectLocation",
    "EvidenceStorageError",
    "ObjectNotFoundError",
    "DigestMismatchError",
    "CollectionRegistry",
    "CollectionEntry",
    "EVIDENCE_HEADER",
    "EvidenceBundle",
    "export_run_evidence",
    "render_evidence_csv",
    "parse_evidence_csv",
    "DownloadSession",
    "TransferItem",
    "TransferInterrupted",
    "PENDING",
    "IN_PROGRESS",
    "DONE",
    "BLOCKED_MISSING",
    "BLOCKED_MISMATCH",
    "CheckResult",
    "VerificationReport",
    "OK",
    "MISSING",
    "MISMATCH",
    "verify_handoff",
    "EvidenceService",
    "get_evidence_service",
]
