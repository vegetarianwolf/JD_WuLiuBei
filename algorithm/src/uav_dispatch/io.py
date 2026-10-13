"""Input helpers for the competition task files."""

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
from typing import Iterable

from .model import Point, Task


_COLUMN_ALIASES = {
    "id": ("任务编号", "task_id", "id"),
    "pickup_x": ("取件点坐标X", "pickup_x"),
    "pickup_y": ("取件点坐标Y", "pickup_y"),
    "delivery_x": ("送达点坐标X", "delivery_x"),
    "delivery_y": ("送达点坐标Y", "delivery_y"),
    "deadline": (
        "最晚送达时间Tmax（min）",
        "最晚送达时间Tmax(min)",
        "deadline_min",
    ),
}


def _decode_csv(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别 CSV 编码: {path}")


def _resolve_columns(fieldnames: Iterable[str] | None) -> dict[str, str]:
    available = {name.strip(): name for name in (fieldnames or ())}
    resolved: dict[str, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in available:
                resolved[canonical] = available[alias]
                break
        else:
            raise ValueError(f"CSV 缺少字段 {canonical}；实际字段为 {list(available)}")
    return resolved


def load_tasks_csv(path: str | Path) -> tuple[Task, ...]:
    """Load either the UTF-8 or Excel-exported GB18030 competition CSV."""

    source = Path(path)
    reader = csv.DictReader(StringIO(_decode_csv(source)))
    columns = _resolve_columns(reader.fieldnames)
    tasks: list[Task] = []
    for row_number, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue
        try:
            tasks.append(
                Task(
                    id=int(row[columns["id"]]),
                    pickup=Point(
                        float(row[columns["pickup_x"]]),
                        float(row[columns["pickup_y"]]),
                    ),
                    delivery=Point(
                        float(row[columns["delivery_x"]]),
                        float(row[columns["delivery_y"]]),
                    ),
                    deadline_min=float(row[columns["deadline"]]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"CSV 第 {row_number} 行数据非法") from exc
    if not tasks:
        raise ValueError("CSV 中没有任务数据")
    return tuple(tasks)
