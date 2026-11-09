"""可选的无头浏览器渲染（Playwright）。

很多公司活动页是纯前端渲染（华为 HDC、百度 Create、火山引擎、WAIC…），
requests 只能拿到空壳，需要真实浏览器执行 JS 后再取 DOM。
Playwright 未安装 / 浏览器未下载时自动降级为普通请求，不阻塞整体运行。
"""

from __future__ import annotations

import atexit
import logging

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class Renderer:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._broken = False
        atexit.register(self.close)

    @staticmethod
    def available() -> bool:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

    def _ensure(self) -> bool:
        if self._browser is not None:
            return True
        if self._broken:
            return False
        try:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            return True
        except Exception as exc:  # noqa: BLE001 - 缺少浏览器/沙箱限制都降级
            log.warning("Playwright 不可用，降级为普通请求：%s", exc)
            self._broken = True
            return False

    def html(self, url: str, *, wait_ms: int = 3000, timeout_ms: int = 45000) -> str | None:
        if not self._ensure():
            return None
        ctx = None
        try:
            ctx = self._browser.new_context(user_agent=UA, locale="zh-CN", viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 - 有的页面永远不 idle，等一会直接取
                pass
            page.wait_for_timeout(wait_ms)
            return page.content()
        except Exception as exc:  # noqa: BLE001
            log.warning("渲染失败 %s: %s", url, str(exc)[:160])
            return None
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._pw = None


_RENDERER: Renderer | None = None


def get_renderer() -> Renderer:
    """整个进程复用一个浏览器实例，避免每条源都冷启动。"""
    global _RENDERER
    if _RENDERER is None:
        _RENDERER = Renderer()
    return _RENDERER
