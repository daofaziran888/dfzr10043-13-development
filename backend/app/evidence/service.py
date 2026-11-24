"""归集证据门面服务：把原件仓、登记台账、导出、续传、验件组装成一套可用能力。

默认工作目录在 backend 运行目录下的 ``var/evidence``（已在 .gitignore 忽略），
测试通过显式传入 root 使用临时目录，互不影响。
"""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Callable

from app.evidence.export import (
    EvidenceBundle,
    export_run_evidence,
    parse_evidence_csv,
)
from app.evidence.handoff import VerificationReport, verify_handoff
from app.evidence.registry import CollectionEntry, CollectionRegistry
from app.evidence.store import ContentAddressedStore, ObjectLocation
from app.evidence.transfer import DownloadSession, TransferInterrupted


class EvidenceService:
    """对外统一入口，路由层只做参数编排，不做业务判断。"""

    def __init__(self, root: str | Path, *,
                 volumes: tuple[str, ...] = ("v1", "v2")) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = ContentAddressedStore(self.root / "store", volumes=volumes)
        self.registry = CollectionRegistry(
            self.store, self.root / "registry" / "registry.json")
        self.sessions_root = self.root / "sessions"
        self.sessions_root.mkdir(parents=True, exist_ok=True)

    # ── 归集与更正 ─────────────────────────────────────────────────
    def collect(self, run_id: str, file_name: str, data: bytes | BinaryIO,
                *, media_type: str = "application/octet-stream",
                declared_digest: str | None = None) -> CollectionEntry:
        return self.registry.collect(
            run_id, file_name, data, media_type=media_type,
            declared_digest=declared_digest)

    def report_correction(self, run_id: str, file_name: str,
                          data: bytes | BinaryIO, *,
                          media_type: str = "application/octet-stream") -> CollectionEntry:
        return self.registry.report_correction(
            run_id, file_name, data, media_type=media_type)

    def list_runs(self) -> list[str]:
        return self.registry.list_runs()

    def list_entries(self, run_id: str | None = None) -> list[CollectionEntry]:
        if run_id is None:
            return self.registry.all_entries()
        return self.registry.entries_for_run(run_id)

    # ── 证据导出 ───────────────────────────────────────────────────
    def export_evidence(self, run_id: str, *, include_superseded: bool = True,
                        on_row: Callable[[CollectionEntry], None] | None = None
                        ) -> EvidenceBundle:
        return export_run_evidence(
            self.registry, self.store, run_id,
            include_superseded=include_superseded, on_row=on_row)

    @staticmethod
    def read_evidence(evidence_csv: str):
        return parse_evidence_csv(evidence_csv)

    # ── 物理路径迁移 / 丢失 / 恢复 ─────────────────────────────────
    def migrate_object(self, digest: str, target_volume: str) -> ObjectLocation:
        return self.store.migrate(digest, target_volume)

    def migrate_entry(self, source_id: str, target_volume: str) -> ObjectLocation:
        entry = self.registry.get_entry(source_id)
        return self.store.migrate(entry.digest, target_volume)

    def mark_lost(self, source_id: str) -> None:
        entry = self.registry.get_entry(source_id)
        self.store.mark_lost(entry.digest)

    def restore_source(self, source_id: str, data: bytes | BinaryIO) -> ObjectLocation:
        entry = self.registry.get_entry(source_id)
        return self.store.restore(entry.digest, data)

    def storage_state(self, source_id: str) -> str:
        entry = self.registry.get_entry(source_id)
        return self.store.state_of(entry.digest)

    # ── 断点续传下载 ───────────────────────────────────────────────
    def start_download(self, evidence_csv: str, signer: str) -> DownloadSession:
        return DownloadSession.start(
            self.sessions_root, self.store, self.registry, evidence_csv, signer)

    def open_download(self, session_id: str) -> DownloadSession:
        return DownloadSession.open(
            self.sessions_root, self.store, self.registry, session_id)

    # ── 交接验件 ───────────────────────────────────────────────────
    def verify(self, evidence_csv: str, *,
               restrict_run: str | None = None) -> VerificationReport:
        return verify_handoff(
            evidence_csv, self.registry, self.store, restrict_run=restrict_run)


_default_service: EvidenceService | None = None


def get_evidence_service() -> EvidenceService:
    """进程级默认实例；测试自行 new EvidenceService(tmp_path)，不依赖此单例。"""
    global _default_service
    if _default_service is None:
        _default_service = EvidenceService(Path("var/evidence"))
    return _default_service
