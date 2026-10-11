"""Telegram API client (sendMessage / setWebhook) using httpx."""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("companion.telegram")


class TelegramClient:
    def __init__(self, bot_token: str, timeout: float = 15.0) -> None:
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def send_message(self, chat_id: int, text: str) -> bool:
        """Send one message; splits long text; retries transient failures once."""
        sent_ok = True
        for chunk in _split(text, 4096):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            for attempt in range(2):
                try:
                    client = await self._get()
                    resp = await client.post(f"{self.base}/sendMessage", json=payload)
                    if resp.status_code == 200:
                        break
                    if resp.status_code == 429:
                        retry = min(int(resp.json().get("parameters", {}).get("retry_after", 2)), 5)
                        await asyncio.sleep(retry)
                        continue
                    # Markdown parse errors -> resend as plain text
                    if resp.status_code == 400 and "parse" in resp.text.lower():
                        payload.pop("parse_mode")
                        continue
                    log.warning("telegram sendMessage failed: status=%s", resp.status_code)
                    sent_ok = False
                    break
                except httpx.HTTPError as exc:
                    log.warning("telegram transport error: %s", type(exc).__name__)
                    if attempt == 1:
                        sent_ok = False
        return sent_ok

    async def set_webhook(self, url: str, secret_token: str) -> dict:
        client = await self._get()
        payload = {
            "url": url.rstrip("/") + "/webhook",
            "secret_token": secret_token,
            "drop_pending_updates": False,
            "allowed_updates": ["message"],
        }
        resp = await client.post(f"{self.base}/setWebhook", json=payload)
        return resp.json()

    async def delete_webhook(self) -> dict:
        client = await self._get()
        resp = await client.post(f"{self.base}/deleteWebhook")
        return resp.json()

    async def get_me(self) -> dict:
        client = await self._get()
        resp = await client.get(f"{self.base}/getMe")
        return resp.json()


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks
