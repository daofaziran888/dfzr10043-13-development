"""内容寻址原件仓：归集原件按 sha256 摘要保存，物理路径可迁移而证据不变。

关键约束（对应题目要求）：
- put 只新增、不覆盖。相同字节复用同一 blob；不同字节必然得到不同摘要，
  因此更正件不可能在任何环节替换旧条目引用的原字节。
- catalog 是「来源证据 → 当前物理位置」的间接层。迁移只改 catalog 定位，
  来源编号与摘要保持不变，导出/下载按 catalog 现势定位取件，迁走旧路径不受影响。
- 迁移先复制到新卷并复核摘要，再原子切换 catalog，最后删除旧物理文件；
  迁移期间 catalog 始终指向可读位置（旧文件尚未删除）。
- mark_lost 模拟原件物理丢失；restore 按原摘要恢复，字节不符一律拒绝。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

CHUNK_SIZE = 64 * 1024


class EvidenceStorageError(Exception):
    """归集证据存储层基础异常。"""


class ObjectNotFoundError(EvidenceStorageError):
    """catalog 中没有该摘要，或原件物理文件已丢失。"""


class DigestMismatchError(EvidenceStorageError):
    """写入/恢复的字节与声明摘要不一致，操作被拒绝。"""


def sha256_file(path: Path) -> str:
    """流式计算文件 sha256，避免大文件一次性进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_stream(handle: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class ObjectLocation:
    """原件在某个存储卷上的物理定位（catalog 记录值）。"""

    volume: str
    relpath: str
    size: int

    def to_dict(self) -> dict[str, object]:
        return {"volume": self.volume, "relpath": self.relpath, "size": self.size}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ObjectLocation":
        return cls(volume=str(data["volume"]), relpath=str(data["relpath"]),
                   size=int(data["size"]))


class ContentAddressedStore:
    """以 sha256 摘要为主键的原件仓，含卷管理、迁移与丢失/恢复支持。"""

    def __init__(self, root: str | Path, *, volumes: tuple[str, ...] = ("v1",)) -> None:
        self.root = Path(root)
        self.volumes = tuple(volumes) or ("v1",)
        self.blobs_dir = self.root / "blobs"
        self.catalog_path = self.root / "catalog.json"
        for volume in self.volumes:
            (self.blobs_dir / volume).mkdir(parents=True, exist_ok=True)
        self._catalog: dict[str, ObjectLocation] = self._load_catalog()

    # ── catalog 持久化（原子替换，避免半写状态） ──────────────────────
    def _load_catalog(self) -> dict[str, ObjectLocation]:
        if not self.catalog_path.exists():
            return {}
        with self.catalog_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return {digest: ObjectLocation.from_dict(item) for digest, item in raw.items()}

    def _save_catalog(self) -> None:
        payload = {
            digest: location.to_dict()
            for digest, location in sorted(self._catalog.items())
        }
        tmp = self.catalog_path.with_name(f".catalog.{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.catalog_path)

    # ── 写入：只新增不覆盖 ───────────────────────────────────────────
    def put(self, data: bytes | BinaryIO, *, declared_digest: str | None = None) -> str:
        """保存一份原件，返回 sha256 摘要。同字节复用，绝不覆盖既有 blob。"""
        if isinstance(data, (bytes, bytearray)):
            digest = hashlib.sha256(data).hexdigest()
            size = len(data)
            stream_data: bytes = bytes(data)
        else:
            digest, size = sha256_stream(data)
            stream_data = b""
        if declared_digest is not None and declared_digest != digest:
            raise DigestMismatchError(
                f"声明摘要 {declared_digest} 与实际字节 {digest} 不一致，拒绝写入")
        if digest in self._catalog:
            existing = self._catalog[digest]
            if self._physical_path(existing).exists():
                # 同字节幂等复用；不同字节在哈希上不可能落到同一 key
                return digest
            # catalog 登记仍在但物理原件已丢失：按既有定位重新落盘（恢复场景）
            location = existing
        else:
            location = self._to_location(digest, size)
        physical = self._physical_path(location)
        physical.parent.mkdir(parents=True, exist_ok=True)
        tmp = physical.with_name(f".{physical.name}.{uuid.uuid4().hex}.tmp")
        try:
            if stream_data:
                tmp.write_bytes(stream_data)
            else:
                with tmp.open("wb") as out:
                    data.seek(0)
                    shutil.copyfileobj(data, out, CHUNK_SIZE)
            # 落盘后再复核一次，防止磁盘层异常导致坏文件进入 catalog
            actual = sha256_file(tmp)
            if actual != digest:
                raise DigestMismatchError(f"落盘复核失败：期望 {digest}，实际 {actual}")
            os.replace(tmp, physical)
        finally:
            if tmp.exists():
                tmp.unlink()
        self._catalog[digest] = location
        self._save_catalog()
        return digest

    def _to_location(self, digest: str, size: int, *, volume: str | None = None) -> ObjectLocation:
        target_volume = volume or self.volumes[0]
        # 两级分片，避免单目录文件过多
        relpath = f"{digest[:2]}/{digest[2:4]}/{digest}"
        return ObjectLocation(volume=target_volume, relpath=relpath, size=size)

    def _physical_path(self, location: ObjectLocation) -> Path:
        return self.blobs_dir / location.volume / location.relpath

    # ── 读取 ────────────────────────────────────────────────────────
    def locate(self, digest: str) -> ObjectLocation:
        location = self._catalog.get(digest)
        if location is None:
            raise ObjectNotFoundError(f"原件仓未登记摘要 {digest}")
        return location

    def open_blob(self, digest: str) -> BinaryIO:
        """按摘要打开原件；物理文件被迁走/丢失时给出明确异常。"""
        location = self.locate(digest)
        physical = self._physical_path(location)
        if not physical.exists():
            raise ObjectNotFoundError(
                f"摘要 {digest} 的物理原件已丢失（卷 {location.volume}，"
                f"路径 {location.relpath}），须按来源编号恢复")
        return physical.open("rb")

    def read_bytes(self, digest: str) -> bytes:
        with self.open_blob(digest) as handle:
            return handle.read()

    def stream_blob(self, digest: str, offset: int = 0) -> Iterator[bytes]:
        """从 offset 开始流式读取，供断点续传复用已签收字节。"""
        with self.open_blob(digest) as handle:
            if offset:
                handle.seek(offset)
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                yield chunk

    def state_of(self, digest: str) -> str:
        """返回 available / lost / unknown，供导出与验件判断。"""
        if digest not in self._catalog:
            return "unknown"
        return "available" if self._physical_path(self._catalog[digest]).exists() else "lost"

    # ── 迁移：复制 → 复核 → 换定位 → 删旧，证据不变 ─────────────────
    def migrate(self, digest: str, target_volume: str) -> ObjectLocation:
        """把原件迁到新卷。迁移后摘要与来源编号不变，旧物理路径被删除。"""
        old = self.locate(digest)
        if old.volume == target_volume:
            return old
        if target_volume not in self.volumes:
            raise EvidenceStorageError(f"未知存储卷 {target_volume}")
        old_path = self._physical_path(old)
        if not old_path.exists():
            raise ObjectNotFoundError(
                f"摘要 {digest} 原件已丢失，无法迁移，须先恢复到卷 {old.volume}")

        new_location = self._to_location(digest, old.size, volume=target_volume)
        new_path = self._physical_path(new_location)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = new_path.with_name(f".{new_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            # 1) 先复制到新卷（旧文件仍在，导出/下载依旧可读）
            with old_path.open("rb") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK_SIZE)
            # 2) 新卷复核摘要，不符立即中止，catalog 不动
            if sha256_file(tmp) != digest:
                raise DigestMismatchError(f"迁移到卷 {target_volume} 后摘要复核失败")
            os.replace(tmp, new_path)
        finally:
            if tmp.exists():
                tmp.unlink()
        # 3) 原子切换 catalog 定位
        self._catalog[digest] = new_location
        self._save_catalog()
        # 4) 确认新定位可读后再删旧物理文件
        if not new_path.exists():  # pragma: no cover - 防御性检查
            raise EvidenceStorageError("迁移后新卷文件缺失，保留旧文件")
        old_path.unlink()
        return new_location

    # ── 丢失与恢复 ─────────────────────────────────────────────────
    def mark_lost(self, digest: str) -> None:
        """模拟原件物理路径被迁走/删除但 catalog 登记仍在（须报告待恢复编号）。"""
        location = self.locate(digest)
        physical = self._physical_path(location)
        if physical.exists():
            physical.unlink()

    def restore(self, digest: str, data: bytes | BinaryIO) -> ObjectLocation:
        """按原摘要恢复原件；字节与摘要不符一律拒绝，防止伪件顶替。"""
        location = self.locate(digest)
        restored_digest = self.put(data, declared_digest=digest)
        if restored_digest != digest:  # pragma: no cover - put 已校验
            raise DigestMismatchError("恢复字节与原摘要不符")
        return self._catalog[digest]

    def known_digests(self) -> list[str]:
        return sorted(self._catalog)
