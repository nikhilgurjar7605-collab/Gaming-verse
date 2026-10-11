"""Webhook endpoint tests: secret validation, dedupe, malformed bodies.

The real FastAPI routes are exercised against an injected AppState that uses
in-memory doubles -- no Postgres, no Telegram, no LLM calls leave the process.
"""

from __future__ import annotations

import httpx
import pytest
from contextlib import asynccontextmanager

from app.chat import ChatOrchestrator
from app.config import Settings
from app.handlers import BotHandler
from app.llm import LLMRateLimitError, LLMTimeoutError
import app.main as main_mod


SECRET = "test-secret"


class _Sink:
    """Fake Telegram sender that records outbound messages."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True

    async def close(self) -> None:
        pass


class _StubLLM:
    def __init__(self) -> None:
        self.error: Exception | None = None

    async def chat(self, messages, *, max_tokens=None, temperature=None):
        if self.error:
            raise self.error
        return "stubbed reply 💬"

    async def close(self) -> None:
        pass


def build_test_app(db):
    """FastAPI app wired to test state instead of the production lifespan."""
    settings = Settings(_env_file=None, webhook_secret_token=SECRET)
    state = main_mod.AppState()
    state.db = db
    sink = _Sink()
    state.llm = _StubLLM()
    state.tg = sink
    orch = ChatOrchestrator(db, state.llm, settings)
    state.handler = BotHandler(db, orch, sink, settings)

    @asynccontextmanager
    async def stub_lifespan(app):
        old = main_mod.state
        main_mod.state = state
        try:
            yield
        finally:
            main_mod.state = old

    test_app = main_mod.FastAPI(lifespan=stub_lifespan)
    for route in main_mod.app.routes:
        test_app.router.routes.append(route)
    return test_app, sink


def make_update(update_id: int = 1, text: str = "hello") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "from": {"id": 42, "first_name": "Zoe"},
            "chat": {"id": 42},
            "date": 1730000000,
            "text": text,
        },
    }


@pytest.fixture()
async def wh_client(db):
    app, sink = build_test_app(db)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c, sink


async def test_webhook_rejects_missing_secret(wh_client):
    client, _sink = wh_client
    r = await client.post("/webhook", json=make_update())
    assert r.status_code == 403


async def test_webhook_rejects_wrong_secret(wh_client):
    client, _sink = wh_client
    r = await client.post("/webhook", json=make_update(),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 403


async def test_webhook_accepts_correct_secret_and_dedupes(wh_client):
    client, sink = wh_client
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    upd = make_update(update_id=777, text="/help")

    r1 = await client.post("/webhook", json=upd, headers=headers)
    assert r1.status_code == 200 and r1.json()["ok"] is True
    assert len(sink.sent) == 1  # help text delivered once

    r2 = await client.post("/webhook", json=upd, headers=headers)
    assert r2.status_code == 200
    assert r2.json().get("ignored") == "duplicate update"
    assert len(sink.sent) == 1, "duplicate update must not re-trigger handling"


async def test_webhook_malformed_json_returns_400(wh_client):
    client, _sink = wh_client
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"}
    r = await client.post("/webhook", content=b"{not json", headers=headers)
    assert r.status_code == 400


async def test_webhook_ignores_updates_without_message(wh_client):
    client, sink = wh_client
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    r = await client.post("/webhook", json={"update_id": 999}, headers=headers)
    assert r.status_code == 200
    assert sink.sent == []


async def test_health_endpoint_reports_db_up(wh_client):
    client, _sink = wh_client
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] == "up"
