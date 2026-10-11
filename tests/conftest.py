"""Shared fixtures for unit tests."""

from __future__ import annotations

import os

# Force safe test env BEFORE app modules read settings.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET_TOKEN", "test-secret")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgres://invalid:invalid@127.0.0.1:1/none")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import pytest  # noqa: E402

from app.chat import ChatOrchestrator  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import InMemoryDB  # noqa: E402
from app.handlers import BotHandler  # noqa: E402


class FakeLLM:
    """Deterministic stand-in for LLMClient."""

    def __init__(self, reply: str = "Hey you! 😄", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, messages, *, max_tokens=None, temperature=None):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.reply

    async def close(self):
        pass


class FakeTG:
    """Collects outbound Telegram messages."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True

    async def close(self):
        pass

    def last(self) -> str:
        return self.sent[-1][1] if self.sent else ""


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture()
def db() -> InMemoryDB:
    return InMemoryDB()


@pytest.fixture()
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture()
def fake_tg() -> FakeTG:
    return FakeTG()


@pytest.fixture()
def handler(db, fake_llm, fake_tg, settings) -> BotHandler:
    orch = ChatOrchestrator(db, fake_llm, settings)
    return BotHandler(db, orch, fake_tg, settings)


def make_update(update_id: int, text: str, tg_id: int = 111, first_name: str = "Sam") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": tg_id, "first_name": first_name, "is_bot": False},
            "chat": {"id": tg_id, "type": "private"},
            "date": 1730000000,
            "text": text,
        },
    }
