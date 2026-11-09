"""入口：抓取 -> 抽取 -> 去重 -> 过滤 -> 发信。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

import yaml

from .extractor import extract_from_leads, extract_from_page
from .fetcher import EVENT_HINT, fetch_gdelt, fetch_page, fetch_rss
from .filtering import fingerprint, is_actionable, is_domestic, merge
from .llm import BudgetExceeded, LLM, LLMError
from .mailer import Mailer, render_html, render_text
from .models import Event, RunStats, Source
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STATE_PATH = ROOT / "state" / "seen_events.json"
ARCHIVE_PATH = ROOT / "docs" / "events.md"

log = logging.getLogger("radar")

SOURCE_TIMEOUT = int(os.getenv("SOURCE_TIMEOUT_SEC", "120"))


class SourceTimeout(Exception):
    """单条源超时，跳过继续跑下一条，避免整轮卡死。"""


def _alarm_handler(signum, frame):  # noqa: ARG001
    raise SourceTimeout()


def run_with_timeout(fn, seconds: int, **kwargs):
    """用 SIGALRM 给单条源加硬超时（socket 阻塞、浏览器卡住都能打断）。"""
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        return fn(**kwargs)
    old = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(seconds)
    try:
        return fn(**kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def load_sources(path: Path) -> list[Source]:
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    out: list[Source] = []
    for row in data.get("sources", []):
        out.append(
            Source(
                id=row["id"],
                name=row.get("name", row["id"]),
                url=row["url"],
                type=row.get("type", "page"),
                region=row.get("region", "global"),
                category=row.get("category", ""),
                enabled=bool(row.get("enabled", True)),
                render=row.get("render", ""),
                aggregator=bool(row.get("aggregator", False)),
                note=row.get("note", ""),
            )
        )
    return out


def load_queries(path: Path) -> dict:
    return yaml.safe_load(path.read_text("utf-8")) or {}


def collect_from_sources(sources: list[Source], llm: LLM, today: date, stats: RunStats, only: set[str] | None) -> list[Event]:
    events: list[Event] = []
    for src in sources:
        if not src.enabled:
            continue
        if only and src.id not in only:
            continue
        stats.sources_total += 1
        t0 = time.time()
        try:
            page = run_with_timeout(
                fetch_page, SOURCE_TIMEOUT, url=src.url, render=src.render == "js"
            )
        except SourceTimeout:
            stats.sources_failed += 1
            stats.errors.append(f"{src.id}: timeout>{SOURCE_TIMEOUT}s")
            log.warning("[%s] 超过 %ss 未返回，跳过", src.id, SOURCE_TIMEOUT)
            continue
        except Exception as exc:  # noqa: BLE001 - 单条源异常不影响整轮
            stats.sources_failed += 1
            stats.errors.append(f"{src.id}: {exc}")
            log.warning("[%s] 抓取异常：%s", src.id, exc)
            continue
        if not page.ok:
            stats.sources_failed += 1
            stats.errors.append(f"{src.id}: {page.error or 'empty page'}")
            log.warning("[%s] 抓取失败：%s", src.id, page.error or "empty")
            continue
        found = extract_from_page(page, src, llm, today)
        stats.sources_ok += 1
        stats.raw_events += len(found)
        events.extend(found)
        log.info("[%s] %d 个活动，%.1fs", src.id, len(found), time.time() - t0)
    return events


def collect_from_search(config: dict, llm: LLM, today: date, stats: RunStats) -> list[Event]:
    queries = config.get("queries") or []
    max_items = int(config.get("max_items_per_query", 8))
    leads: list[dict[str, str]] = []
    seen_links: set[str] = set()
    last_gdelt = 0.0
    for q in queries:
        if q.get("enabled", True) is False:
            continue
        qtype = q.get("type", "rss")
        if qtype == "gdelt":
            # GDELT 官方限制：1 请求 / 5 秒
            gap = time.time() - last_gdelt
            if gap < 6:
                time.sleep(6 - gap)
            items = fetch_gdelt(q["q"], timespan=q.get("timespan", "7d"), maxrecords=max_items)
            last_gdelt = time.time()
        else:
            url = q["url"].format(query=quote_plus(q["q"])) if "{query}" in q.get("url", "") else q["url"]
            items = fetch_rss(url, limit=max_items)
        if not items:
            stats.errors.append(f"search:{q['name']}: no items")
            continue
        stats.sources_total += 1
        stats.sources_ok += 1
        for item in items:
            # 预过滤：标题/摘要必须同时出现「活动词」和「报名/开放」信号
            blob = f"{item['title']} {item['summary']}"
            if not EVENT_HINT.search(blob):
                continue
            if not any(k in blob for k in ("报名", "注册", "开放", "register", "Registration", "Sign up", "门票", "ticket")):
                continue
            if item["link"] in seen_links:
                continue
            seen_links.add(item["link"])
            leads.append(item)
        log.info("[search:%s] %d 条线索（累计 %d）", q.get("name", "?"), len(items), len(leads))

    if not leads:
        return []
    log.info("线索共 %d 条，交给 LLM 筛选", len(leads))
    found = extract_from_leads(leads[: int(config.get("max_leads", 40))], llm, today)
    stats.raw_events += len(found)
    return found


def dedupe(events: list[Event]) -> list[Event]:
    merged: dict[str, Event] = {}
    for ev in events:
        ev.fingerprint = fingerprint(ev)
        if ev.fingerprint in merged:
            merge(merged[ev.fingerprint], ev)
        else:
            merged[ev.fingerprint] = ev
    return list(merged.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI / 技术活动雷达")
    parser.add_argument("--dry-run", action="store_true", help="不写状态、不发邮件")
    parser.add_argument("--no-mail", action="store_true", help="只抓取，不发邮件")
    parser.add_argument("--only", default="", help="只跑指定 source id，逗号分隔")
    parser.add_argument("--max-sources", type=int, default=0, help="最多跑多少个来源（调试用）")
    parser.add_argument("--no-search", action="store_true", help="跳过网络检索兜底")
    parser.add_argument("--dump", default="", help="把本轮抽取到的全部活动写成 JSON（调试用）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    today = date.today()
    stats = RunStats()

    sources = load_sources(CONFIG_DIR / "sources.yml")
    if args.max_sources:
        sources = sources[: args.max_sources]
    search_cfg = load_queries(CONFIG_DIR / "queries.yml")
    settings = load_queries(CONFIG_DIR / "settings.yml")
    scope = str(settings.get("scope", "all")).lower()
    max_per_mail = int(settings.get("max_per_mail", 18))
    unknown_cap = int(settings.get("unknown_cap", 8))
    log.info("推送范围：%s（国内=cn / 全部=all）", "仅国内" if scope == "cn" else "全球")
    only = {x.strip() for x in args.only.split(",") if x.strip()} or None

    llm = LLM(max_calls=int(os.getenv("LLM_MAX_CALLS", "60")))
    if not llm.enabled:
        log.warning("未配置 DEEPSEEK_API_KEY，只能跳过 LLM 抽取（不会产出结果）")

    events: list[Event] = []
    try:
        events += collect_from_sources(sources, llm, today, stats, only)
        if not args.no_search and not only:
            events += collect_from_search(search_cfg, llm, today, stats)
    except BudgetExceeded as exc:
        log.warning("LLM 预算用尽，提前收工：%s", exc)

    deduped = dedupe(events)
    log.info("去重后 %d 个活动（原始 %d）", len(deduped), len(events))
    if args.dump:
        Path(args.dump).write_text(
            json.dumps([e.to_dict() for e in deduped], ensure_ascii=False, indent=1), "utf-8"
        )
        log.info("已导出 %d 个活动到 %s", len(deduped), args.dump)

    store = Store(STATE_PATH, ARCHIVE_PATH)
    aggregator_ids = {s.id for s in sources if s.aggregator}
    candidates: list[Event] = []

    for ev in deduped:
        rec = store.get(ev.fingerprint)
        # 只要国内：境外场次一律只记录、不推送
        if scope == "cn" and not is_domestic(ev, ev.source_region):
            store.upsert(ev, notified=False)
            continue
        if not is_actionable(ev, today):
            # 已满/已截止/未开启/已结束：只更新记录，不打扰
            if rec is not None:
                store.upsert(ev, notified=False)
            continue
        # 「状态待确认」只信任一手官方源，且必须有日期；聚合平台上的一律不进邮件
        if ev.registration_status == "unknown" and (ev.source_id in aggregator_ids or not ev.start_date):
            store.upsert(ev, notified=False)
            continue
        if rec is None or rec.get("notified_status") != ev.registration_status:
            candidates.append(ev)
        else:
            store.upsert(ev, notified=False)

    # 明确开放报名的全部发；待确认的最多 8 条，按日期取最近的，压制噪音
    open_evs = [e for e in candidates if e.registration_status == "open"]
    unknown_evs = sorted(
        [e for e in candidates if e.registration_status == "unknown"],
        key=lambda e: (e.start_date or "9999", e.title),
    )[:unknown_cap]
    to_notify = open_evs + unknown_evs
    to_notify.sort(key=lambda e: (e.registration_deadline or "9999", e.start_date or "9999"))

    # 单封邮件最多 N 条，避免刷屏；其余仍会写进归档
    stats.omitted = max(0, len(to_notify) - max_per_mail)
    all_selected = list(to_notify)
    to_notify = to_notify[:max_per_mail]

    selected = {e.fingerprint for e in all_selected}
    for ev in deduped:
        if ev.fingerprint in selected:
            store.upsert(ev, notified=True)
            stats.new_events += 1
        elif store.get(ev.fingerprint) is None:
            store.upsert(ev, notified=False)

    stats.notified = len(to_notify)
    stats.llm_calls = llm.calls

    print(json.dumps({"stats": stats.to_dict(), "llm": llm.stats(), "notified": [e.title for e in to_notify]}, ensure_ascii=False, indent=2))

    if args.dry_run:
        for ev in to_notify:
            log.info("[dry-run] 待通知：%s | %s | %s", ev.title, ev.status_label, ev.registration_url)
        return 0

    if not args.no_mail and to_notify:
        subject = f"[AI 活动雷达] {len(to_notify)} 个可报名活动 · {today.strftime('%m-%d')}"
        ok = Mailer().send(subject, render_html(to_notify, today, stats.to_dict()), render_text(to_notify))
        if not ok:
            log.error("邮件失败，状态仍会保存，避免下轮重复推送")
    elif not to_notify:
        log.info("没有新的可报名活动，今天不发信")

    store.prune(today)
    store.save()
    store.render_archive(today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
