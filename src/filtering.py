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

# ---- 地域判定（scope=cn 时用）----
CN_PLACE = re.compile(
    r"(中国|国内|大陆|香港|澳门|大湾区|长三角|珠三角|雄安|"
    r"北京|上海|广州|深圳|杭州|成都|南京|武汉|西安|苏州|天津|重庆|长沙|合肥|厦门|青岛|郑州|济南|"
    r"福州|南昌|昆明|贵阳|南宁|海口|太原|石家庄|沈阳|大连|哈尔滨|长春|兰州|银川|西宁|乌鲁木齐|"
    r"呼和浩特|拉萨|珠海|东莞|佛山|宁波|无锡|常州|温州|嘉兴|绍兴|金华|泉州|"
    r"广东|浙江|江苏|四川|湖北|陕西|山东|福建|河南|湖南|安徽|江西|云南|贵州|广西|河北|山西|辽宁|"
    r"吉林|黑龙江|甘肃|宁夏|青海|新疆|西藏|内蒙古|海南|台湾|"
    r"\bchina\b|\bgreater china\b|\bbeijing\b|\bshanghai\b|\bshenzhen\b|\bhangzhou\b|\bguangzhou\b|"
    r"\bchengdu\b|\bnanjing\b|\bwuhan\b|\bhong kong\b|\bmacau\b|\bmacao\b|\bsuzhou\b|\bxiamen\b|"
    r"\bqingdao\b|\bxi'?an\b|\btianjin\b|\bchongqing\b|\bchangsha\b|\bhefei\b|\bzhuhai\b|\bdongguan\b)",
    re.I,
)
CN_ORG = re.compile(
    r"(阿里|蚂蚁|腾讯|华为|百度|字节|火山引擎|小米|讯飞|京东|美团|滴滴|快手|网易|智源|CCF|"
    r"中国计算机学会|掘金|SegmentFault|InfoQ|开源中国|OPPO|vivo|商汤|旷视|浪潮|中兴|"
    r"中国移动|中国电信|中国联通|之江实验室|上海人工智能实验室|面壁|月之暗面|智谱|MiniMax|"
    r"深度求索|DeepSeek|通义|文心|混元|魔搭|ModelScope)",
    re.I,
)
ONLINE_ONLY = re.compile(r"^(线上|online|virtual|网络|云端|云上)$", re.I)


def is_domestic(ev: Event, source_region: str = "") -> bool:
    """判断活动是否在国内举办（中国大陆 + 港澳）。

    规则顺序：
    1. 标题/地点/摘要里出现中国地名 → 国内（覆盖「PyTorch Day China」这类海外机构在国内的场次）
    2. 有具体地点但没中国字样 → 境外
    3. 线上或地点为空 → 看主办方是不是国内机构，再看来源站是不是国内站
    """
    text = f"{ev.title} {ev.location or ''} {ev.summary or ''}"
    if CN_PLACE.search(text):
        return True
    loc = (ev.location or "").strip()
    if loc and not ONLINE_ONLY.search(loc):
        return False
    if CN_ORG.search(ev.organizer or ""):
        return True
    return source_region == "cn"


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
