"""证据导出：按执行导出带来源编号与 sha256 校验值的证据件。

与平台通用列表导出（resource.py 的清单 CSV）不同，证据件的每一行都锚定
「哪次执行、哪个来源编号、什么文件名、多少字节、什么摘要、是否已被更正」，
交接包据此可以按实际归集内容逐件验件，而不只是拿到一份清单索引。

``on_row`` 回调在每一行落盘前触发，用于模拟「导出过程中正赶上原件物理路径
被迁走」：迁移只改原件仓 catalog 定位，导出仍按 catalog 现势定位取件，不失败。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Callable, Iterable

from app.evidence.registry import CollectionEntry, CollectionRegistry
from app.evidence.store import ContentAddressedStore

# 证据 CSV 列顺序即对外契约，旧索引按这些列名回读
EVIDENCE_HEADER = [
    "source_id", "run_id", "seq", "file_name", "media_type",
    "size", "sha256", "status", "supersedes", "revision", "collected_at",
]

RowCallback = Callable[[CollectionEntry], None]


@dataclass(frozen=True)
class EvidenceRow:
    """证据件中的一行，与一条归集来源记录一一对应。"""

    source_id: str
    run_id: str
    seq: int
    file_name: str
    media_type: str
    size: int
    sha256: str
    status: str
    supersedes: str
    revision: int
    collected_at: str

    def to_csv_row(self) -> list[object]:
        return [
            self.source_id, self.run_id, self.seq, self.file_name, self.media_type,
            self.size, self.sha256, self.status, self.supersedes, self.revision,
            self.collected_at,
        ]

    @classmethod
    def from_entry(cls, entry: CollectionEntry) -> "EvidenceRow":
        return cls(
            source_id=entry.source_id, run_id=entry.run_id, seq=entry.seq,
            file_name=entry.file_name, media_type=entry.media_type, size=entry.size,
            sha256=entry.digest, status=entry.status,
            supersedes=entry.supersedes or "", revision=entry.evidence_revision,
            collected_at=entry.collected_at,
        )

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "EvidenceRow":
        return cls(
            source_id=str(row["source_id"]), run_id=str(row["run_id"]),
            seq=int(row["seq"]), file_name=str(row["file_name"]),
            media_type=str(row.get("media_type", "")), size=int(row["size"]),
            sha256=str(row["sha256"]), status=str(row.get("status", "active")),
            supersedes=str(row.get("supersedes", "")),
            revision=int(row.get("revision", "1")),
            collected_at=str(row.get("collected_at", "")),
        )


@dataclass(frozen=True)
class EvidenceBundle:
    """一次导出的结果：CSV 文本、行视图以及导出时各原件的可读状态。"""

    run_id: str
    csv_text: str
    rows: tuple[EvidenceRow, ...]
    storage_state: dict[str, str]   # source_id -> available/lost/unknown

    @property
    def source_ids(self) -> list[str]:
        return [row.source_id for row in self.rows]


def render_evidence_csv(rows: Iterable[EvidenceRow]) -> str:
    buffer = io.StringIO()
    buffer.write("﻿")  # 与平台通用导出保持一致，Excel 打开中文不乱码
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EVIDENCE_HEADER)
    for row in rows:
        writer.writerow(row.to_csv_row())
    return buffer.getvalue()


def export_run_evidence(
    registry: CollectionRegistry,
    store: ContentAddressedStore,
    run_id: str,
    *,
    include_superseded: bool = True,
    on_row: RowCallback | None = None,
) -> EvidenceBundle:
    """按执行导出证据件。旧版本（superseded）默认一并导出，保留可验性。"""
    entries = registry.entries_for_run(run_id, include_superseded=include_superseded)
    rows: list[EvidenceRow] = []
    storage_state: dict[str, str] = {}
    for entry in entries:
        if on_row is not None:
            # 行落盘前回调：此处迁移物理路径只改 catalog，不影响后续取件定位
            on_row(entry)
        rows.append(EvidenceRow.from_entry(entry))
        storage_state[entry.source_id] = store.state_of(entry.digest)
    return EvidenceBundle(
        run_id=run_id, csv_text=render_evidence_csv(rows),
        rows=tuple(rows), storage_state=storage_state,
    )


def parse_evidence_csv(csv_text: str) -> list[EvidenceRow]:
    """交接侧读入证据件：容忍 BOM，按契约列名解析，缺列直接报错。"""
    cleaned = csv_text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(cleaned))
    if reader.fieldnames is None:
        raise ValueError("证据件为空，缺少表头")
    missing = [column for column in EVIDENCE_HEADER if column not in reader.fieldnames]
    if missing:
        raise ValueError(f"证据件缺少必需列：{'、'.join(missing)}")
    return [EvidenceRow.from_csv_row(row) for row in reader]
