"""状态存档（state/seen_events.json）与活动归档（docs/events.md）。"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from .models import Event

log = logging.getLogger(__name__)


class Store:
    def __init__(self, state_path: Path, archive_path: Path) -> None:
        self.state_path = state_path
        self.archive_path = archive_path
        self.records: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
            self.records = data.get("events", {}) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("状态文件读取失败，按空处理: %s", exc)
            self.records = {}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "events": self.records,
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")

    def get(self, fp: str) -> dict | None:
        return self.records.get(fp)

    def upsert(self, ev: Event, *, notified: bool) -> None:
        rec: dict | None = self.records.get(ev.fingerprint)
        now = datetime.now().isoformat(timespec="seconds")
        if rec is None:
            rec = {"first_seen": now, "notified_status": "", "notified_at": ""}
            self.records[ev.fingerprint] = rec
        rec.update(
            {
                "title": ev.title,
                "organizer": ev.organizer,
                "start_date": ev.start_date,
                "end_date": ev.end_date,
                "location": ev.location,
                "online": ev.online,
                "category": ev.category,
                "registration_url": ev.registration_url,
                "registration_status": ev.registration_status,
                "registration_deadline": ev.registration_deadline,
                "requirements": ev.requirements,
                "fee": ev.fee,
                "summary": ev.summary,
                "source_id": ev.source_id,
                "source_url": ev.source_url,
                "last_seen": now,
            }
        )
        if notified:
            rec["notified_status"] = ev.registration_status
            rec["notified_at"] = now

    def prune(self, today: date, keep_days: int = 180) -> int:
        """清理早已结束的记录，避免文件无限膨胀。"""
        removed = 0
        for fp, rec in list(self.records.items()):
            last = rec.get("end_date") or rec.get("start_date")
            if not last:
                continue
            try:
                if (today - date.fromisoformat(last)).days > keep_days:
                    del self.records[fp]
                    removed += 1
            except ValueError:
                continue
        return removed

    def render_archive(self, today: date) -> None:
        """生成人可读的 Markdown 归档：仍在可报名窗口内的排前面。"""
        rows = []
        for rec in self.records.values():
            rows.append(rec)

        def sort_key(r: dict):
            ended = (r.get("end_date") or r.get("start_date") or "9999-99-99")
            return (ended, r.get("title", ""))

        rows.sort(key=sort_key)
        lines = [
            "# AI / 技术活动归档",
            "",
            f"最后更新：{datetime.now().strftime('%Y-%m-%d %H:%M')} · 共 {len(rows)} 条",
            "",
            "| 活动 | 主办 | 时间 | 形式 | 报名状态 | 链接 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in rows:
            period = r.get("start_date") or "?"
            if r.get("end_date") and r.get("end_date") != r.get("start_date"):
                period += f" ~ {r['end_date']}"
            mode = "线上" if r.get("online") else (r.get("location") or "—")
            status = {
                "open": "✅ 报名中",
                "unknown": "❓ 待确认",
                "full": "⛔ 已满",
                "closed": "⛔ 已截止",
                "upcoming": "🕒 未开启",
            }.get(r.get("registration_status", ""), r.get("registration_status", ""))
            title = (r.get("title") or "").replace("|", "/")
            org = (r.get("organizer") or "").replace("|", "/")
            url = r.get("registration_url") or r.get("source_url") or ""
            link = f"[报名/详情]({url})" if url else "—"
            lines.append(f"| {title} | {org} | {period} | {mode} | {status} | {link} |")
        lines.append("")
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_path.write_text("\n".join(lines), "utf-8")
