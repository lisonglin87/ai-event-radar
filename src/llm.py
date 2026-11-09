"""DeepSeek（OpenAI 兼容）调用封装：JSON 模式、重试、预算控制。"""

from __future__ import annotations

import json
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


class LLM:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_calls: int = 60,
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL
        self.max_calls = max_calls
        self.timeout = timeout
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.enabled = bool(self.api_key)

    @property
    def available(self) -> bool:
        return self.enabled and self.calls < self.max_calls

    def chat_json(self, system: str, user: str, *, retries: int = 1) -> dict:
        if not self.enabled:
            raise LLMError("未配置 DEEPSEEK_API_KEY")
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"本次运行 LLM 调用已达上限 {self.max_calls}")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": 2400,
            "response_format": {"type": "json_object"},
        }
        last_err = ""
        for attempt in range(retries + 1):
            self.calls += 1
            try:
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(payload),
                    timeout=self.timeout,
                )
                if r.status_code == 402:
                    raise BudgetExceeded("DeepSeek 余额不足")
                if r.status_code >= 400:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                        time.sleep(2 * (attempt + 1))
                        continue
                    raise LLMError(last_err)
                data = r.json()
                usage = data.get("usage") or {}
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return json.loads(content)
            except (requests.RequestException, json.JSONDecodeError) as exc:
                last_err = str(exc)
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise LLMError(last_err) from exc
        raise LLMError(last_err or "unknown")

    def stats(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
