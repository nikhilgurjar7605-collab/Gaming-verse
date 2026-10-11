"""OpenAI-compatible chat-completions adapter built on httpx.

Handles timeouts, rate limits (429 with Retry-After), malformed responses and
a bounded retry budget -- hosted free tiers are NOT unlimited, so we fail
gracefully instead of hammering the provider.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

log = logging.getLogger("companion.llm")


class LLMError(Exception):
    """Base error surfaced to the orchestrator."""


class LLMAuthError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


@dataclass
class LLMClient:
    base_url: str
    api_key: str
    model: str
    timeout: float = 30.0
    max_retries: int = 2
    temperature: float = 0.85
    max_tokens: int = 500
    client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            )
        return self.client

    async def close(self) -> None:
        if self.client and not self.client.is_closed:
            await self.client.aclose()

    async def chat(self, messages: list[dict], *, max_tokens: int | None = None,
                   temperature: float | None = None) -> str:
        """Return assistant text for a chat-completions request."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                client = await self._get_client()
                resp = await client.post("/chat/completions", json=payload)
                if resp.status_code == 200:
                    return self._extract_text(resp)
                if resp.status_code in (401, 403):
                    raise LLMAuthError(f"provider rejected credentials ({resp.status_code})")
                if resp.status_code == 429:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    # Honor Retry-After but cap the wait; don't burn the whole budget.
                    wait = min(retry_after if retry_after else (1.5 * (attempt + 1)), 8.0)
                    log.warning("llm rate limited; waiting %.1fs (attempt %d/%d)",
                                wait, attempt + 1, self.max_retries + 1)
                    last_exc = LLMRateLimitError("rate limited by provider")
                    if attempt < self.max_retries:
                        await asyncio.sleep(wait + random.uniform(0, 0.4))
                        continue
                    raise last_exc
                if 500 <= resp.status_code < 600:
                    last_exc = LLMError(f"provider error {resp.status_code}")
                else:
                    raise LLMResponseError(f"unexpected status {resp.status_code}")
            except (httpx.TimeoutException,) as exc:
                last_exc = LLMTimeoutError("provider timed out")
                log.warning("llm timeout (attempt %d/%d)", attempt + 1, self.max_retries + 1)
            except (LLMAuthError, LLMRateLimitError, LLMResponseError):
                raise
            except httpx.HTTPError as exc:
                last_exc = LLMError(f"transport error: {type(exc).__name__}")
            if attempt < self.max_retries:
                await asyncio.sleep(0.8 * (attempt + 1) + random.uniform(0, 0.3))
        raise last_exc or LLMError("unknown llm failure")

    @staticmethod
    def _extract_text(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.error("malformed llm response: %s", type(exc).__name__)
            raise LLMResponseError("malformed provider response") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("empty provider reply")
        return content.strip()


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
