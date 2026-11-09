"""把页面 / 搜索线索交给 LLM，抽成结构化活动。"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urlparse

from .fetcher import Page
from .llm import LLM, LLMError
from .models import Event, Source

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是科技活动情报分析员。你的唯一任务：从给定文本中抽取【AI / 计算机 / 软件 / 芯片 / 云计算 / 数据】相关的线下或线上活动，并判断其报名状态。

严格规则：
1. 只抽取文本中真实出现的活动，绝不编造。文本没提到的日期、地点、链接一律留空或 null。
2. 只收「活动」：开发者大会、技术峰会、开发者日、发布会、技术沙龙、meetup、黑客松/比赛、官方技术直播。排除：课程/培训招生、录播系列（Bootcamp、for Beginners、Tutorial、Learning Path、Course、系列课）、认证考试、招聘、产品促销（双11/618/优惠券）、政策新闻、榜单、纯产品功能介绍、征稿（Call for Papers / Posters / Speakers / Submissions / CFP）、往年已结束的活动。
3. 若文本只是新闻或活动回顾、没有可参加的活动，返回 {"events": []}。
4. registration_status 只能取以下值，判断依据必须能在文本里找到：
   - "open"：明确出现报名/注册入口（报名、立即报名、注册参会、Register、Sign up、购票、申请参会）且没有截止或满员字样
   - "full"：出现「名额已满 / 已满员 / 报名人数已满 / sold out / registration full」
   - "closed"：出现「报名已截止 / 已结束 / 停止报名 / registration closed / 活动已结束」
   - "upcoming"：出现「即将开放 / 敬请期待 / 报名将于X开启 / coming soon」
   - "unknown"：文本信息不足以判断
5. start_date / end_date / registration_deadline 用 YYYY-MM-DD；只写年或月无法确定具体日期时填 null，并在 summary 里说明。今天是 __TODAY__。
6. 已确定结束、且早于今天的活动不要输出。
7. registration_url 只能从用户给出的 LINKS 列表里挑选最匹配的报名/详情页；没有合适的填 null。不要拼接或猜测 URL。
8. 每个活动必须有不同的 title；同一活动不要重复输出。
9. 最多输出 6 个活动，按「报名开放优先」排序。

输出严格 JSON：
{"events":[{"title":"","organizer":"","category":"开发者大会|技术峰会|开发者日|发布会|技术沙龙|黑客松|训练营|线上直播","start_date":null,"end_date":null,"location":"","online":false,"registration_url":null,"registration_status":"open","registration_deadline":null,"requirements":"","fee":"","summary":"","evidence":""}]}

evidence 必须是原文逐字摘录（≤80 字），用来支撑 registration_status 的判断。requirements 写报名条件（如「需企业邮箱」「限学生」「需审核」「免费」），没有就留空字符串。"""


def _links_block(page: Page, limit: int = 45) -> str:
    rows = []
    for label, url in page.links[:limit]:
        rows.append(f"- {label} -> {url}")
    return "\n".join(rows)


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


def _sanitize_url(url: str | None, page: Page) -> str:
    """只接受页面里真实出现过的链接，否则退回页面本身。"""
    if not url:
        return page.url
    url = url.strip()
    if not url.startswith("http"):
        return page.url
    allowed = {u for _, u in page.links}
    if url in allowed:
        return url
    # 允许同一域名下的链接（LLM 偶尔会去掉查询串）
    h = _host(url)
    for _, u in page.links:
        if _host(u) == h and h:
            return u
    return page.url


def _clean_date(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"\s*(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", value)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def extract_from_page(page: Page, source: Source, llm: LLM, today: date) -> list[Event]:
    if not page.ok or len(page.text) < 200:
        log.info("跳过（正文过短，可能是 JS 渲染）：%s", source.url)
        return []

    user = (
        f"今天：{today.isoformat()}\n"
        f"来源页面：{page.title} ({page.url})\n"
        f"主办方提示：{source.name}\n\n"
        f"=== 页面正文 ===\n{page.text}\n\n"
        f"=== LINKS ===\n{_links_block(page)}\n"
    )

    try:
        data = llm.chat_json(SYSTEM_PROMPT.replace("__TODAY__", today.isoformat()), user)
    except LLMError as exc:
        log.warning("LLM 抽取失败 %s: %s", source.id, exc)
        return []

    events: list[Event] = []
    for raw in (data.get("events") or [])[:6]:
        title = (raw.get("title") or "").strip()
        if len(title) < 4:
            continue
        ev = Event(
            title=title,
            organizer=(raw.get("organizer") or source.name).strip(),
            start_date=_clean_date(raw.get("start_date")),
            end_date=_clean_date(raw.get("end_date")),
            location=(raw.get("location") or "").strip(),
            online=bool(raw.get("online")),
            registration_url=_sanitize_url(raw.get("registration_url"), page),
            registration_status=(raw.get("registration_status") or "unknown").strip().lower(),
            registration_deadline=_clean_date(raw.get("registration_deadline")),
            requirements=(raw.get("requirements") or "").strip()[:300],
            fee=(raw.get("fee") or "").strip()[:120],
            category=(raw.get("category") or source.category or "").strip(),
            summary=(raw.get("summary") or "").strip()[:400],
            evidence=(raw.get("evidence") or "").strip()[:200],
            source_id=source.id,
            source_name=source.name,
            source_url=source.url,
        )
        if ev.registration_status not in {"open", "upcoming", "full", "closed", "unknown"}:
            ev.registration_status = "unknown"
        events.append(ev)
    return events


RSS_SYSTEM = """你是科技活动情报分析员。输入是一批新闻/搜索结果（标题+摘要+链接）。
任务：找出其中【可报名参加】的 AI/计算机相关活动（大会、峰会、开发者日、黑客松、训练营、官方技术活动）。

规则：
- 只保留明确是「某公司/机构举办的、对外开放报名的活动」，且摘要或标题里出现报名/注册/开放报名/Registration/Sign up/门票等信号。
- 排除：纯新闻解读、产品发布但无活动、课程招生广告、招聘、榜单、会议纪要、已结束的回顾报道。
- 无法确认报名状态时用 "unknown"，不要猜 "open"。
- 日期只在标题/摘要明确出现时填写，否则 null。今天是 __TODAY__。

输出 JSON：{"events":[{"title":"","organizer":"","category":"","start_date":null,"end_date":null,"location":"","online":false,"registration_url":"","registration_status":"open|upcoming|full|closed|unknown","registration_deadline":null,"requirements":"","fee":"","summary":"","evidence":""}]}
registration_url 必须是输入中给出的原始链接之一。没有合适活动返回 {"events": []}。"""


def extract_from_leads(leads: list[dict[str, str]], llm: LLM, today: date, *, batch_size: int = 8) -> list[Event]:
    """把 RSS 线索分批交给 LLM，抽成活动。"""
    events: list[Event] = []
    for i in range(0, len(leads), batch_size):
        batch = leads[i : i + batch_size]
        lines = []
        for idx, item in enumerate(batch):
            lines.append(
                f"[{idx}] {item['title']}\n"
                f"    摘要: {item.get('summary', '')[:220]}\n"
                f"    链接: {item['link']}\n"
                f"    时间: {item.get('published', '')}"
            )
        user = "今天：%s\n\n线索列表：\n%s\n" % (today.isoformat(), "\n".join(lines))
        try:
            data = llm.chat_json(RSS_SYSTEM.replace("__TODAY__", today.isoformat()), user)
        except LLMError as exc:
            log.warning("线索抽取失败: %s", exc)
            continue
        for raw in (data.get("events") or [])[:5]:
            title = (raw.get("title") or "").strip()
            if len(title) < 4:
                continue
            url = (raw.get("registration_url") or "").strip()
            if not url.startswith("http"):
                url = batch[0]["link"] if batch else ""
            events.append(
                Event(
                    title=title,
                    organizer=(raw.get("organizer") or "").strip(),
                    start_date=_clean_date(raw.get("start_date")),
                    end_date=_clean_date(raw.get("end_date")),
                    location=(raw.get("location") or "").strip(),
                    online=bool(raw.get("online")),
                    registration_url=url,
                    registration_status=(raw.get("registration_status") or "unknown").strip().lower(),
                    registration_deadline=_clean_date(raw.get("registration_deadline")),
                    requirements=(raw.get("requirements") or "").strip()[:300],
                    fee=(raw.get("fee") or "").strip()[:120],
                    category=(raw.get("category") or "").strip(),
                    summary=(raw.get("summary") or "").strip()[:400],
                    evidence=(raw.get("evidence") or "").strip()[:200],
                    source_id="web-search",
                    source_name="网络检索",
                    source_url=url,
                )
            )
    return events
