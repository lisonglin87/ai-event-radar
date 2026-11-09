"""去重指纹与「是否值得通知」的判定。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date

from .models import Event

# 主办方后缀噪音，归一化时去掉
ORG_NOISE = re.compile(
    r"(有限公司|股份|集团|inc\.?|ltd\.?|llc|corp\.?|corporation|co\.?|中国|china|技术团队|开发者关系|developer relations)$",
    re.I,
)

TITLE_NOISE = re.compile(
    r"(\d{4}\s*年?|第[一二三四五六七八九十\d]+届|线上|线下|直播|峰会|大会|conference|summit)",
    re.I,
)
PUNCT = re.compile(r"[\s\-_·•.,，。:：;；!！?？/\\|()（）\[\]【】<>《》\"'“”‘’]+")


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = TITLE_NOISE.sub("", t)
    t = ORG_NOISE.sub("", t.strip())
    t = PUNCT.sub("", t)
    return t.strip()


def fingerprint(ev: Event) -> str:
    """同一活动在多个来源出现时指纹应一致。

    以「标题核心 + 月份」为主键：标题里通常已含品牌（NVIDIA GTC、云栖大会），
    主办方字段在各来源间写法差异太大（NVIDIA / NVIDIA GTC / 英伟达），不适合做主键。
    标题过短或过于通用时，才把主办方拼进去降低碰撞概率。
    """
    title = norm(ev.title)
    when = ev.start_date or ev.end_date or ""
    key = f"{title[:28]}|{when[:7]}"
    if len(title) < 8:
        key = f"{norm(ev.organizer)}|{key}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


ACTIONABLE_STATUSES = {"open", "unknown"}

YEAR_RE = re.compile(r"(20\d{2})")


def stale_year(ev: Event, today: date) -> bool:
    """标题里写死过去年份的活动（如 2025 年的录播系列）直接判为过期。"""
    for text in (ev.title, ev.registration_deadline or ""):
        for y in YEAR_RE.findall(text or ""):
            if int(y) < today.year:
                return True
    return False


def is_actionable(ev: Event, today: date) -> bool:
    """是否「现在还能报名」。已结束、已截止、名额已满都不算。"""
    if ev.registration_status in {"closed", "full", "upcoming"}:
        return False
    if ev.registration_status not in ACTIONABLE_STATUSES:
        return False
    if stale_year(ev, today):
        return False
    if ev.expired(today):
        return False
    if ev.deadline_passed(today):
        return False
    return True


def merge(base: Event, other: Event) -> None:
    """把同一活动的多来源信息合并到 base（字段级补全，状态以更明确者为准）。"""
    priority = {"open": 5, "full": 4, "closed": 3, "upcoming": 2, "unknown": 1}
    for field_name in (
        "start_date",
        "end_date",
        "location",
        "registration_url",
        "registration_deadline",
        "requirements",
        "fee",
        "category",
        "summary",
        "organizer",
    ):
        cur = getattr(base, field_name)
        new = getattr(other, field_name)
        if (not cur) and new:
            setattr(base, field_name, new)
    if priority.get(other.registration_status, 0) > priority.get(base.registration_status, 0):
        base.registration_status = other.registration_status
        base.evidence = other.evidence or base.evidence
    if other.online and not base.online:
        base.online = True
