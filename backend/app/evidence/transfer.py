"""断点续传下载：下载者凭证据件逐件取回原件，中断后签收人可凭原证据续验。

会话以证据件快照为不可变输入：
- 每个下载项固定 (source_id, sha256)，取件始终按来源编号在登记台账中定位
  该条目自己的摘要；同名更正件是另一个 source_id，无法替换在途内容。
- 部分下载按字节 offset 续传，恢复时先用原件前缀复核已写部分的 sha256，
  防止把损坏/被改过的半截文件当成已签收字节。
- 同一证据件重复跑不会把同一件计成两条缺件：DONE 项幂等跳过，
  BLOCKED 项在下次恢复时重新判定（原件修复后即可成功）。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from app.evidence.export import parse_evidence_csv
from app.evidence.registry import CollectionRegistry
from app.evidence.store import (
    CHUNK_SIZE,
    ContentAddressedStore,
    EvidenceStorageError,
    ObjectNotFoundError,
)

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
BLOCKED_MISSING = "blocked_missing"
BLOCKED_MISMATCH = "blocked_mismatch"

BLOCKED_STATES = (BLOCKED_MISSING, BLOCKED_MISMATCH)


class TransferInterrupted(EvidenceStorageError):
    """传输预算耗尽触发的可恢复中断（不是失败，会话可续传）。"""


@dataclass
class TransferItem:
    source_id: str
    run_id: str
    file_name: str
    expected_digest: str
    expected_size: int
    state: str = PENDING
    offset: int = 0
    bytes_written: int = 0
    target_relpath: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TransferItem":
        return cls(
            source_id=str(data["source_id"]), run_id=str(data["run_id"]),
            file_name=str(data["file_name"]), expected_digest=str(data["expected_digest"]),
            expected_size=int(data["expected_size"]), state=str(data.get("state", PENDING)),
            offset=int(data.get("offset", 0)), bytes_written=int(data.get("bytes_written", 0)),
            target_relpath=str(data.get("target_relpath", "")), detail=str(data.get("detail", "")),
        )


def _safe_name(name: str) -> str:
    """把归集文件名收敛成安全的落盘名，目录穿越字符一律替换。"""
    cleaned = name.replace("\\", "_").replace("/", "_").replace("..", "_")
    return cleaned or "unnamed"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DownloadSession:
    """一次可暂停、可恢复的下载会话，状态持久化在会话目录下。"""

    def __init__(self, sessions_root: str | Path, store: ContentAddressedStore,
                 registry: CollectionRegistry, session_id: str, signer: str,
                 evidence_csv: str, items: list[TransferItem]) -> None:
        self.sessions_root = Path(sessions_root)
        self.store = store
        self.registry = registry
        self.session_id = session_id
        self.signer = signer
        self.evidence_csv = evidence_csv          # 原证据件快照，续传不重新取索引
        self.items = items
        self.session_dir = self.sessions_root / session_id
        self.payload_dir = self.session_dir / "payloads"
        self.state_path = self.session_dir / "session.json"
        self.payload_dir.mkdir(parents=True, exist_ok=True)

    # ── 生命周期 ───────────────────────────────────────────────────
    @classmethod
    def start(cls, sessions_root: str | Path, store: ContentAddressedStore,
              registry: CollectionRegistry, evidence_csv: str, signer: str
              ) -> "DownloadSession":
        rows = parse_evidence_csv(evidence_csv)
        session_id = f"DL-{uuid.uuid4().hex[:12]}"
        items = [
            TransferItem(
                source_id=row.source_id, run_id=row.run_id, file_name=row.file_name,
                expected_digest=row.sha256, expected_size=row.size,
                target_relpath=f"{row.run_id}/{row.source_id}/{_safe_name(row.file_name)}",
            )
            for row in rows
        ]
        session = cls(sessions_root, store, registry, session_id, signer,
                      evidence_csv, items)
        session._save()
        return session

    @classmethod
    def open(cls, sessions_root: str | Path, store: ContentAddressedStore,
             registry: CollectionRegistry, session_id: str) -> "DownloadSession":
        state_path = Path(sessions_root) / session_id / "session.json"
        if not state_path.exists():
            raise ObjectNotFoundError(f"下载会话 {session_id} 不存在")
        with state_path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        items = [TransferItem.from_dict(item) for item in state["items"]]
        return cls(
            sessions_root, store, registry, session_id,
            signer=str(state["signer"]), evidence_csv=str(state["evidence_csv"]),
            items=items,
        )

    def _save(self) -> None:
        state = {
            "session_id": self.session_id,
            "signer": self.signer,
            "evidence_csv": self.evidence_csv,
            "items": [item.to_dict() for item in self.items],
        }
        tmp = self.state_path.with_name(f".session.{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.state_path)

    # ── 传输主循环 ─────────────────────────────────────────────────
    def run(self, *, byte_budget: int | None = None) -> dict[str, object]:
        """推进下载。预算耗尽抛 TransferInterrupted，已写字节与状态已落盘可续。

        byte_budget 是「本次调用」的传输预算，不跨调用持久化：不传即不限量，
        一次跑完所有待下载项；中断后恢复用无参 run() 即可继续。
        """
        remaining = byte_budget
        interrupted = False
        for item in self.items:
            if item.state == DONE:
                continue  # 幂等：重复核验不重复计缺件
            if item.state in BLOCKED_STATES:
                # 恢复重试：原件可能已修复。阻塞件的半截文件不可信，作废从 0 重下
                item.state = PENDING
                item.detail = ""
                item.offset = 0
                item.bytes_written = 0
                stale = self.payload_dir / item.target_relpath
                stale.with_name(stale.name + ".part").unlink(missing_ok=True)
            try:
                remaining = self._download_item(item, remaining)
            except TransferInterrupted:
                interrupted = True
                break
        self._save()
        if interrupted:
            summary = self.summary()
            raise TransferInterrupted(
                f"会话 {self.session_id} 传输预算耗尽，已签收 "
                f"{summary['done']}/{summary['total']} 件，可凭原证据恢复")
        return self.summary()

    def _verify_resume_prefix(self, item: TransferItem, part_path: Path) -> int:
        """复核半截文件与原件前缀一致，返回可用偏移；损坏则丢弃从 0 重下。

        原件物理丢失时不在这里抛异常：返回已有偏移让主流程统一报 BLOCKED_MISSING，
        并明确给出待恢复的来源编号。
        """
        if not part_path.exists() or part_path.stat().st_size == 0:
            return 0
        size = part_path.stat().st_size
        local_digest = _sha256_path(part_path)
        prefix = hashlib.sha256()
        consumed = 0
        try:
            handle = self.store.open_blob(item.expected_digest)
        except ObjectNotFoundError:
            return size  # 主流程随后取流时报告缺件，已写前缀原样保留
        with handle:
            while consumed < size:
                chunk = handle.read(min(CHUNK_SIZE, size - consumed))
                if not chunk:
                    break
                prefix.update(chunk)
                consumed += len(chunk)
        if consumed != size or prefix.hexdigest() != local_digest:
            part_path.unlink()
            return 0
        return size

    def _download_item(self, item: TransferItem,
                       remaining: int | None) -> int | None:
        target = self.payload_dir / item.target_relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        part_path = target.with_name(target.name + ".part")

        # 按来源编号定位登记条目，取该条目固定摘要——更正件无法顶替
        try:
            entry = self.registry.get_entry(item.source_id)
        except ObjectNotFoundError as exc:
            item.state = BLOCKED_MISSING
            item.detail = str(exc)
            return remaining
        if entry.digest != item.expected_digest:
            # 来源编号仍在但其摘要被改（正常架构不会发生）→ 拒绝，防止中途替换
            item.state = BLOCKED_MISMATCH
            item.detail = (f"来源 {item.source_id} 摘要 {entry.digest} 与证据 "
                           f"{item.expected_digest} 不符，拒绝在途替换")
            return remaining

        resume_offset = self._verify_resume_prefix(item, part_path)
        item.offset = resume_offset
        item.bytes_written = resume_offset
        item.state = IN_PROGRESS
        self._save()

        try:
            # stream_blob 是生成器，物理文件在迭代时才打开，
            # 因此缺件异常必须覆盖整个流式读取段，而不只是取得生成器那一刻
            stream = self.store.stream_blob(item.expected_digest, offset=resume_offset)
            digest = hashlib.sha256()
            if resume_offset:
                # 续传时把已落盘前缀纳入最终摘要计算
                with part_path.open("rb") as existing:
                    for chunk in iter(lambda: existing.read(CHUNK_SIZE), b""):
                        digest.update(chunk)

            with part_path.open("ab") as out:
                for chunk in stream:
                    if remaining is not None and remaining <= 0:
                        # 预算已耗尽：当前块一字节都不能再写，留待恢复
                        raise TransferInterrupted("达到本次传输预算")
                    if remaining is not None and len(chunk) > remaining:
                        # 预算只够块的前半段：写允许部分后即中断，
                        # 已写字节落盘，恢复时按 offset 从原件同位置续取
                        head = chunk[:remaining]
                        out.write(head)
                        digest.update(head)
                        item.bytes_written += len(head)
                        remaining -= len(head)
                        item.offset = item.bytes_written
                        self._save()
                        raise TransferInterrupted("达到本次传输预算")
                    if remaining is not None:
                        remaining -= len(chunk)
                    out.write(chunk)
                    digest.update(chunk)
                    item.bytes_written += len(chunk)
                    item.offset = item.bytes_written
        except ObjectNotFoundError as exc:
            # 原件物理路径被迁走/丢失：明确报告尚须恢复的来源编号
            item.state = BLOCKED_MISSING
            item.detail = f"待恢复原归集编号 {item.source_id}：{exc}"
            return remaining
        except TransferInterrupted:
            item.state = IN_PROGRESS
            self._save()
            raise
        self._save()

        # 整件复核：摘要或字节数不符绝不签收
        if digest.hexdigest() != item.expected_digest:
            item.state = BLOCKED_MISMATCH
            item.detail = "下载完成但整体 sha256 与证据不符，拒绝签收"
            part_path.unlink(missing_ok=True)
            item.bytes_written = item.offset = 0
            return remaining
        if item.bytes_written != item.expected_size:
            item.state = BLOCKED_MISMATCH
            item.detail = f"字节数 {item.bytes_written} 与证据 {item.expected_size} 不符"
            part_path.unlink(missing_ok=True)
            item.bytes_written = item.offset = 0
            return remaining

        os.replace(part_path, target)
        item.state = DONE
        item.detail = ""
        return remaining

    # ── 状态与签收 ─────────────────────────────────────────────────
    def payload_path(self, source_id: str) -> Path:
        return self.payload_dir / self.item(source_id).target_relpath

    def item(self, source_id: str) -> TransferItem:
        for item in self.items:
            if item.source_id == source_id:
                return item
        raise ObjectNotFoundError(f"会话 {self.session_id} 无来源 {source_id}")

    def is_complete(self) -> bool:
        return all(item.state == DONE for item in self.items)

    def summary(self) -> dict[str, object]:
        counts = {PENDING: 0, IN_PROGRESS: 0, DONE: 0,
                  BLOCKED_MISSING: 0, BLOCKED_MISMATCH: 0}
        for item in self.items:
            counts[item.state] = counts.get(item.state, 0) + 1
        return {
            "session_id": self.session_id,
            "signer": self.signer,
            "total": len(self.items),
            "done": counts[DONE],
            "pending": counts[PENDING] + counts[IN_PROGRESS],
            "blocked_missing": counts[BLOCKED_MISSING],
            "blocked_mismatch": counts[BLOCKED_MISMATCH],
            "complete": counts[DONE] == len(self.items),
            "missing_source_ids": [
                item.source_id for item in self.items if item.state == BLOCKED_MISSING],
            "mismatch_source_ids": [
                item.source_id for item in self.items if item.state == BLOCKED_MISMATCH],
        }
