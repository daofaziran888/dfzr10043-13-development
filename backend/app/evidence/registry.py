"""归集登记：按执行实例登记来源编号，同名更正只追加，原字节永不消失。

来源编号 source_id 形如 ``RUN-A#0003``，由「执行编号 + 执行内单调序号」组成，
全局唯一且永不复用。机器人补报同名更正件时：
- 旧条目不删除、不覆盖，状态由 active 改为 superseded（仍被旧任务/旧证据引用）；
- 新内容因字节不同而在原件仓得到新摘要，登记为一条全新来源编号；
- 两条记录的 ``文件名`` 可以相同，但来源编号和摘要不同，证据行可指出内容来自哪次归集。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

from app.evidence.store import (
    ContentAddressedStore,
    DigestMismatchError,
    ObjectNotFoundError,
)

ACTIVE = "active"
SUPERSEDED = "superseded"
_CN_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(_CN_TZ).isoformat(timespec="seconds")


@dataclass
class CollectionEntry:
    """一次归集动作产生的一条来源记录（更正件是新记录，不是改旧记录）。"""

    source_id: str
    run_id: str
    seq: int
    file_name: str
    digest: str
    size: int
    media_type: str
    collected_at: str
    status: str = ACTIVE
    supersedes: str | None = None          # 更正件指向被替换的旧来源编号
    evidence_revision: int = 1             # 同一执行内证据版本，更正一次 +1
    history: list[str] = field(default_factory=list)  # 状态流转留痕

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "CollectionEntry":
        return cls(
            source_id=str(data["source_id"]),
            run_id=str(data["run_id"]),
            seq=int(data["seq"]),
            file_name=str(data["file_name"]),
            digest=str(data["digest"]),
            size=int(data["size"]),
            media_type=str(data.get("media_type", "application/octet-stream")),
            collected_at=str(data["collected_at"]),
            status=str(data.get("status", ACTIVE)),
            supersedes=data.get("supersedes") or None,
            evidence_revision=int(data.get("evidence_revision", 1)),
            history=[str(item) for item in data.get("history", [])],
        )


class CollectionRegistry:
    """执行 → 来源条目的登记台账，持久化为 JSON，原件保存在内容寻址仓。"""

    def __init__(self, store: ContentAddressedStore, registry_path: str | Path) -> None:
        self.store = store
        self.registry_path = Path(registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, CollectionEntry] = self._load()

    def _load(self) -> dict[str, CollectionEntry]:
        if not self.registry_path.exists():
            return {}
        with self.registry_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return {
            item["source_id"]: CollectionEntry.from_dict(item)
            for item in raw["entries"]
        }

    def _save(self) -> None:
        payload = {"entries": [entry.to_dict() for entry in self._entries_sorted()]}
        tmp = self.registry_path.with_name(f".registry.{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.registry_path)

    def _entries_sorted(self) -> list[CollectionEntry]:
        return sorted(self._entries.values(), key=lambda e: (e.run_id, e.seq, e.source_id))

    def list_runs(self) -> list[str]:
        return sorted({entry.run_id for entry in self._entries.values()})

    def _next_seq(self, run_id: str) -> int:
        used = [entry.seq for entry in self._entries.values() if entry.run_id == run_id]
        return (max(used) + 1) if used else 1

    def _make_source_id(self, run_id: str, seq: int) -> str:
        return f"{run_id}#{seq:04d}"

    def collect(self, run_id: str, file_name: str, data: bytes | BinaryIO,
                *, media_type: str = "application/octet-stream",
                declared_digest: str | None = None) -> CollectionEntry:
        """登记一次新归集。来源编号唯一不复用，原件进内容寻址仓只新增。"""
        seq = self._next_seq(run_id)
        source_id = self._make_source_id(run_id, seq)
        try:
            digest = self.store.put(data, declared_digest=declared_digest)
        except DigestMismatchError:
            raise
        size = self.store.locate(digest).size
        revision = 1 + sum(1 for e in self._entries.values() if e.run_id == run_id
                           and e.file_name == file_name)
        entry = CollectionEntry(
            source_id=source_id, run_id=run_id, seq=seq, file_name=file_name,
            digest=digest, size=size, media_type=media_type,
            collected_at=_now_iso(), evidence_revision=revision,
            history=[f"{_now_iso()} 登记归集 {source_id}"],
        )
        self._entries[source_id] = entry
        self._save()
        return entry

    def report_correction(self, run_id: str, file_name: str, data: bytes | BinaryIO,
                          *, media_type: str = "application/octet-stream") -> CollectionEntry:
        """机器人补报同名更正件：旧条目保留转 superseded，新内容另立来源编号。"""
        active_same_name = [
            entry for entry in self._entries.values()
            if entry.run_id == run_id and entry.file_name == file_name
            and entry.status == ACTIVE
        ]
        if not active_same_name:
            raise ObjectNotFoundError(
                f"执行 {run_id} 下没有名为 {file_name} 的在册原件，无法补报更正件")
        new_entry = self.collect(run_id, file_name, data, media_type=media_type)
        for old in active_same_name:
            old.status = SUPERSEDED
            old.history.append(f"{_now_iso()} 被更正件 {new_entry.source_id} 替代，原字节保留")
        new_entry.supersedes = active_same_name[-1].source_id
        new_entry.history.append(
            f"{_now_iso()} 作为更正件替代 {new_entry.supersedes}（旧来源编号继续可验）")
        self._save()
        return new_entry

    def get_entry(self, source_id: str) -> CollectionEntry:
        entry = self._entries.get(source_id)
        if entry is None:
            raise ObjectNotFoundError(f"来源编号 {source_id} 未登记")
        return entry

    def find_by_name(self, run_id: str, file_name: str,
                     *, status: str | None = None) -> list[CollectionEntry]:
        """按执行+文件名定位，同名多版本全部返回；status 可限定 active/superseded。"""
        return [
            entry for entry in self._entries.values()
            if entry.run_id == run_id and entry.file_name == file_name
            and (status is None or entry.status == status)
        ]

    def entries_for_run(self, run_id: str, *, include_superseded: bool = True
                        ) -> list[CollectionEntry]:
        """导出/验件按执行取条目；默认含 superseded，保证旧任务引用仍可验。"""
        rows = [entry for entry in self._entries_sorted() if entry.run_id == run_id]
        if not include_superseded:
            rows = [entry for entry in rows if entry.status == ACTIVE]
        return rows

    def all_entries(self) -> list[CollectionEntry]:
        return self._entries_sorted()

    def open_source(self, source_id: str) -> BinaryIO:
        """凭来源编号打开对应原件（按该条目固定摘要取件，不会被更正件顶替）。"""
        entry = self.get_entry(source_id)
        return self.store.open_blob(entry.digest)
