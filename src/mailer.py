"""邮件发送：QQ 邮箱 SMTP（SSL 465 / STARTTLS 587）。"""

from __future__ import annotations

import html
import logging
import os
import smtplib
from datetime import date
from email.header import Header
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from .models import Event

log = logging.getLogger(__name__)


class Mailer:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        recipients: list[str] | None = None,
        ssl_mode: bool | None = None,
    ) -> None:
        self.host = host or os.getenv("SMTP_HOST", "smtp.qq.com")
        self.port = int(port or os.getenv("SMTP_PORT", "465"))
        self.user = user or os.getenv("SMTP_USER", "")
        self.password = password or os.getenv("SMTP_PASSWORD", "") or os.getenv("QQ_SMTP_AUTH_CODE", "")
        self.sender = sender or os.getenv("MAIL_FROM", self.user)
        raw_to = os.getenv("MAIL_TO", self.sender)
        self.recipients = recipients or [x.strip() for x in raw_to.split(",") if x.strip()]
        self.ssl_mode = ssl_mode if ssl_mode is not None else self.port == 465

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.recipients)

    def send(self, subject: str, html_body: str, text_body: str = "") -> bool:
        if not self.configured:
            log.error("邮件未配置：缺少 SMTP_USER / SMTP_PASSWORD / MAIL_TO")
            return False
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((str(Header("AI 活动雷达", "utf-8")), self.sender))
        msg["To"] = ", ".join(self.recipients)
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(text_body or "请使用支持 HTML 的邮件客户端查看。")
        msg.add_alternative(html_body, subtype="html")

        try:
            if self.ssl_mode:
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=30)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=30)
                server.starttls()
            with server:
                server.login(self.user, self.password)
                server.sendmail(self.sender, self.recipients, msg.as_string())
            log.info("邮件已发送：%s -> %s", subject, self.recipients)
            return True
        except Exception as exc:  # noqa: BLE001 - SMTP 异常种类多，统一记录
            log.error("邮件发送失败：%s", exc)
            return False


def _fmt_period(ev: Event) -> str:
    if ev.start_date and ev.end_date and ev.start_date != ev.end_date:
        return f"{ev.start_date} ~ {ev.end_date}"
    return ev.start_date or ev.end_date or "时间待定"


def _fmt_mode(ev: Event) -> str:
    if ev.online and ev.location:
        return f"线上 + {ev.location}"
    if ev.online:
        return "线上"
    return ev.location or "地点待定"


def render_html(events: list[Event], today: date, stats: dict[str, int]) -> str:
    cards = []
    for ev in events:
        badge_color = "#0b7a3b" if ev.registration_status == "open" else "#8a6d00"
        badge_bg = "#e6f6ec" if ev.registration_status == "open" else "#fdf3d6"
        rows = [
            ("时间", _fmt_period(ev)),
            ("形式", _fmt_mode(ev)),
            ("主办", ev.organizer or "—"),
            ("报名条件", ev.requirements or "页面未写明（通常无门槛）"),
            ("费用", ev.fee or "页面未写明"),
        ]
        if ev.registration_deadline:
            rows.insert(1, ("报名截止", ev.registration_deadline))
        detail = "".join(
            f'<tr><td style="padding:2px 10px 2px 0;color:#6b7280;white-space:nowrap;'
            f'font-size:13px;vertical-align:top">{k}</td>'
            f'<td style="padding:2px 0;color:#111827;font-size:13px">{html.escape(str(v))}</td></tr>'
            for k, v in rows
        )
        summary = (
            f'<p style="margin:8px 0 0;font-size:13px;color:#374151;line-height:1.6">'
            f"{html.escape(ev.summary)}</p>"
            if ev.summary
            else ""
        )
        evidence = (
            f'<p style="margin:6px 0 0;font-size:12px;color:#9ca3af">依据：{html.escape(ev.evidence)}</p>'
            if ev.evidence and len(ev.evidence) >= 12
            else ""
        )
        btn = (
            f'<a href="{html.escape(ev.registration_url)}" style="display:inline-block;margin-top:10px;'
            f'background:#111827;color:#fff;text-decoration:none;padding:8px 14px;border-radius:6px;'
            f'font-size:13px">打开报名 / 详情页</a>'
            if ev.registration_url
            else ""
        )
        cards.append(
            f'<div style="border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:0 0 12px">'
            f'<div style="font-size:15px;font-weight:600;color:#111827;line-height:1.4">'
            f"{html.escape(ev.title)}</div>"
            f'<span style="display:inline-block;margin-top:6px;padding:2px 8px;border-radius:999px;'
            f'background:{badge_bg};color:{badge_color};font-size:12px">{html.escape(ev.status_label)}</span>'
            f"{summary}"
            f'<table style="margin-top:10px;border-collapse:collapse">{detail}</table>'
            f"{evidence}{btn}</div>"
        )

    stat_line = (
        f"扫描 {stats.get('sources_ok', 0)}/{stats.get('sources_total', 0)} 个来源 · "
        f"抽取 {stats.get('raw_events', 0)} 个活动 · 本次新增可报名 {stats.get('notified', 0)} 个"
    )
    if stats.get("omitted"):
        stat_line += f" · 另有 {stats['omitted']} 个已记录在仓库 docs/events.md"
    fail_note = ""
    if stats.get("sources_failed"):
        fail_note = (
            f'<p style="margin:6px 0 0;font-size:12px;color:#9ca3af">'
            f'{stats["sources_failed"]} 个来源本次抓取失败，下轮会重试。</p>'
        )
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:640px;margin:0 auto">
  <h1 style="margin:0 0 4px;font-size:19px;color:#111827">今天有 {len(events)} 个还能报名的 AI / 技术活动</h1>
  <p style="margin:0 0 18px;font-size:13px;color:#6b7280">{today.isoformat()} · 只推送「新出现」或「刚开放报名」的活动</p>
  {''.join(cards)}
  <p style="margin:16px 0 0;font-size:12px;color:#9ca3af">{stat_line}</p>
  {fail_note}
  <p style="margin:6px 0 0;font-size:12px;color:#9ca3af">报名状态由页面文本自动判断，最终以活动官网为准。</p>
</div></body></html>"""


def render_text(events: list[Event]) -> str:
    lines = []
    for ev in events:
        lines.append(f"■ {ev.title}（{ev.organizer}）")
        lines.append(f"  时间：{_fmt_period(ev)}  形式：{_fmt_mode(ev)}")
        lines.append(f"  状态：{ev.status_label}")
        if ev.requirements:
            lines.append(f"  条件：{ev.requirements}")
        lines.append(f"  链接：{ev.registration_url}")
        lines.append("")
    return "\n".join(lines)
