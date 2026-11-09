"""HTTP 抓取与正文/链接抽取。"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 正文里值得送进模型的信号
EVENT_HINT = re.compile(
    r"(报名|注册|register|registration|sign\s*up|门票|ticket|名额|"
    r"大会|峰会|论坛|开发者日|开发者大会|发布会|沙龙|黑客松|hackathon|"
    r"conference|summit|workshop|meetup|webinar|bootcamp|challenge|"
    r"open\s*day|dev\s*day|devday)",
    re.I,
)
YEAR_HINT = re.compile(r"(20[2-3]\d)")
DATE_HINT = re.compile(
    r"(\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{1,2})",
    re.I,
)

DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "nav", "footer", "header", "form")

try:  # lxml 更快；装不上时退回标准库解析器
    import lxml  # noqa: F401

    PARSER = "lxml"
except ImportError:  # pragma: no cover
    PARSER = "html.parser"


@dataclass
class Page:
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)   # (anchor text, abs url)
    ok: bool = False
    error: str = ""


def _client() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.max_redirects = 5
    return s


SESSION = _client()

_dns_cache: dict[str, bool] = {}

_reach_cache: dict[str, bool] = {}


def _resolve_ok(url: str, timeout: float = 8.0) -> bool:
    """域名解析 + TCP 可达性探测（带超时、按 host 缓存）。

    被墙/黑洞的域名（Google 系）在 macOS 上不仅 DNS 会卡，
    requests 还会对解析出的每个 IP 逐个 connect（10s × N 个 IP），
    所以这里先用 4 秒的单点探测判死刑，避免整轮被拖死。
    """
    host = urlparse(url).hostname or ""
    if not host:
        return False
    if host in _reach_cache:
        return _reach_cache[host]

    box: list[bool] = []

    def work() -> None:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except OSError:
            box.append(False)
            return
        for info in infos[:2]:  # 只试前两个地址，够用且够快
            sock = socket.socket(info[0], socket.SOCK_STREAM)
            sock.settimeout(4)
            try:
                sock.connect(info[4])
                box.append(True)
                return
            except OSError:
                continue
            finally:
                sock.close()
        box.append(False)

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout * 2)
    if not box:  # 探测本身卡住（极少数情况），不缓存，下次再来
        return False
    _reach_cache[host] = box[0]
    return box[0]


def get(url: str, *, timeout: int = 25, retries: int = 2, backoff: float = 1.5) -> requests.Response | None:
    if not _resolve_ok(url):
        log.warning("域名解析失败/超时，跳过 %s", url)
        return None
    last = None
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code >= 500 and attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            return r
        except Exception as exc:  # noqa: BLE001 - 网络异常都当可重试
            last = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    log.warning("GET 失败 %s: %s", url, last)
    return None


def _text_of(node) -> str:
    t = node.get_text(" ", strip=True)
    t = re.sub(r"\s+", " ", t)
    return t


def _pick_blocks(soup: BeautifulSoup) -> list[str]:
    """挑选可能承载活动信息的文本块。"""
    blocks: list[str] = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "div", "span", "a"]):
        if tag.find(["p", "li", "div", "table"]):
            continue  # 只取叶子节点，避免整段重复
        t = _text_of(tag)
        if not (8 <= len(t) <= 400):
            continue
        blocks.append(t)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for b in blocks:
        if b in seen:
            continue
        seen.add(b)
        out.append(b)
    return out


def parse_html(url: str, html: str, *, max_text: int = 5000) -> Page:
    soup = BeautifulSoup(html, PARSER)
    title = _text_of(soup.title) if soup.title else ""

    for tag in soup(DROP_TAGS):
        tag.decompose()

    blocks = _pick_blocks(soup)
    strong = [b for b in blocks if EVENT_HINT.search(b) or DATE_HINT.search(b) or YEAR_HINT.search(b)]
    weak = [b for b in blocks if b not in set(strong)]

    # 强信号块优先，弱信号块少量补足上下文
    text_parts: list[str] = []
    size = 0
    for b in strong + weak[:60]:
        if size + len(b) > max_text:
            break
        text_parts.append(b)
        size += len(b) + 1

    # 链接：报名入口最可能在 <a> 里
    links: list[tuple[str, str]] = []
    link_seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        absu = urljoin(url, href)
        if not absu.startswith("http"):
            continue
        label = _text_of(a) or absu
        if len(label) > 90:
            label = label[:90]
        key = f"{label}|{absu}"
        if key in link_seen:
            continue
        link_seen.add(key)
        links.append((label, absu))
        if len(links) >= 80:
            break

    return Page(url=url, final_url=url, title=title, text="\n".join(text_parts), links=links, ok=bool(text_parts))


def fetch_page(url: str, *, timeout: int = 25, render: bool = False) -> Page:
    if not _resolve_ok(url):
        return Page(url=url, ok=False, error="host unreachable")
    r = get(url, timeout=timeout)
    page: Page | None = None
    if r is not None and r.status_code < 400:
        if r.encoding in (None, "ISO-8859-1"):
            r.encoding = r.apparent_encoding or "utf-8"
        page = parse_html(r.url, r.text)
        page.final_url = r.url

    # 静态抓不到正文（纯前端渲染）时才动用无头浏览器，避免每条源都变慢
    if render and (page is None or len(page.text) < 400):
        from .render import get_renderer

        html = get_renderer().html(url)
        if html:
            rendered = parse_html(url, html)
            rendered.final_url = url
            if len(rendered.text) > len(page.text if page else ""):
                return rendered
        if page is not None:
            return page

    if page is None:
        return Page(url=url, ok=False, error="request failed" if r is None else f"HTTP {r.status_code}")
    return page


def fetch_rss(url: str, *, limit: int = 15) -> list[dict[str, str]]:
    """极简 RSS 解析：返回 [{title, link, summary, published}]。"""
    r = get(url, timeout=20)
    if r is None or r.status_code >= 400:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        log.warning("RSS 解析失败 %s", url)
        return []
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", desc)[:400]
        if title and link:
            items.append({"title": title, "link": link, "summary": desc, "published": pub})
        if len(items) >= limit:
            break
    return items


def fetch_gdelt(query: str, *, timespan: str = "7d", maxrecords: int = 20) -> list[dict[str, str]]:
    """GDELT 全球新闻库（免费、无需 key，限制 1 请求/5 秒）。

    作为 Google News RSS 的备份：部分网络环境访问不了 Google，GDELT 一般都能通。
    """
    from urllib.parse import quote_plus as _qp

    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc?"
        f"query={_qp(query)}&mode=artlist&maxrecords={maxrecords}"
        f"&format=json&timespan={timespan}&sort=datedesc"
    )
    r = get(url, timeout=30, retries=1)
    if r is None or r.status_code != 200:
        log.warning("GDELT 请求失败 %s: %s", query, getattr(r, "status_code", "?"))
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    out: list[dict[str, str]] = []
    for art in data.get("articles", []) or []:
        title = (art.get("title") or "").strip()
        link = (art.get("url") or "").strip()
        if not title or not link:
            continue
        out.append(
            {
                "title": title,
                "link": link,
                "summary": (art.get("domain") or "").strip(),
                "published": (art.get("seendate") or "").strip(),
            }
        )
    return out
