"""核心数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any


# 报名状态：
#   open      = 报名开放中
#   upcoming  = 报名尚未开启（预告/待开放）
#   full      = 名额已满
#   closed    = 报名已截止 / 活动已结束
#   unknown   = 页面上看不出来，需人工确认
STATUSES = ("open", "upcoming", "full", "closed", "unknown")

STATUS_LABEL = {
    "open": "报名开放中",
    "upcoming": "报名未开启",
    "full": "名额已满",
    "closed": "已截止/已结束",
    "unknown": "状态待确认",
}


@dataclass
class Event:
    title: str
    organizer: str
    start_date: str | None = None          # YYYY-MM-DD
    end_date: str | None = None            # YYYY-MM-DD
    location: str = ""
    online: bool = False
    registration_url: str = ""
    registration_status: str = "unknown"
    registration_deadline: str | None = None
    requirements: str = ""
    fee: str = ""
    category: str = ""
    summary: str = ""
    evidence: str = ""                     # LLM 引用原文，用于人工核对
    source_id: str = ""
    source_name: str = ""
    source_url: str = ""
    source_region: str = ""                # cn | global（来自来源配置）

    # 运行时填充
    fingerprint: str = ""
    first_seen: str = ""
    last_seen: str = ""
    notified_status: str = ""
    notified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.registration_status, self.registration_status)

    def expired(self, today: date) -> bool:
        """活动是否已经过去。优先用结束日期，其次开始日期。"""
        for raw in (self.end_date, self.start_date):
            if not raw:
                continue
            try:
                return date.fromisoformat(raw) < today
            except ValueError:
                continue
        return False

    def deadline_passed(self, today: date) -> bool:
        if not self.registration_deadline:
            return False
        try:
            return date.fromisoformat(self.registration_deadline) < today
        except ValueError:
            return False


@dataclass
class Source:
    id: str
    name: str
    url: str
    type: str = "page"                     # page | rss
    region: str = "global"                 # global | cn
    category: str = ""
    enabled: bool = True
    render: str = ""                       # "js" = 需要无头浏览器渲染
    aggregator: bool = False               # true = 聚合平台（活动行/活动索引等），可信度低于官方源
    note: str = ""


@dataclass
class RunStats:
    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    llm_calls: int = 0
    raw_events: int = 0
    new_events: int = 0
    notified: int = 0
    omitted: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
