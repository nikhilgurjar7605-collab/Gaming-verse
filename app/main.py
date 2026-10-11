"""FastAPI application: Telegram webhook endpoint, health checks, lifespan wiring.

Run locally:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.chat import ChatOrchestrator
from app.config import get_settings
from app.db import Database, InMemoryDB
from app.handlers import BotHandler
from app.llm import LLMClient
from app.telegram_api import TelegramClient

log = logging.getLogger("companion")


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
    # Never let secrets leak through third-party debug logs.
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class AppState:
    """Mutable container filled during lifespan startup."""

    def __init__(self) -> None:
        self.db = None
        self.llm = None
        self.tg = None
        self.handler: BotHandler | None = None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)

    # --- database -----------------------------------------------------------
    try:
        state.db = await Database.connect(settings.database_url)
        log.info("connected to postgres")
    except Exception as exc:  # noqa: BLE001
        log.error("postgres unavailable (%s); falling back to in-memory store "
                  "(data will NOT persist across restarts)", type(exc).__name__)
        state.db = InMemoryDB()

    # --- llm ------------------------------------------------------------------
    state.llm = LLMClient(
        base_url=settings.llm_api_base,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )

    # --- telegram -------------------------------------------------------------
    state.tg = TelegramClient(settings.telegram_bot_token)
    orchestrator = ChatOrchestrator(state.db, state.llm, settings)
    state.handler = BotHandler(state.db, orchestrator, state.tg, settings)

    if settings.telegram_bot_token and settings.webhook_url and settings.webhook_secret_token:
        try:
            result = await state.tg.set_webhook(settings.webhook_url,
                                                settings.webhook_secret_token)
            log.info("webhook registration result ok=%s", result.get("ok"))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not register webhook at startup: %s", type(exc).__name__)
    else:
        log.warning("telegram webhook env vars incomplete; webhook auto-registration skipped")

    yield

    for closer in (state.llm.close, state.tg.close, state.db.close):
        try:
            await closer()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(title="ai-companion-bot", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root() -> dict:
    return {"service": "ai-companion-bot", "status": "running",
            "docs": "/docs", "health": "/health"}


@app.get("/health")
async def health() -> JSONResponse:
    db_ok = False
    if state.db is not None:
        db_ok = await state.db.healthcheck()
    code = 200 if db_ok else 503
    return JSONResponse(status_code=code,
                        content={"status": "ok" if db_ok else "degraded",
                                 "database": "up" if db_ok else "down"})


@app.post("/webhook")
async def webhook(request: Request,
                  x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict:
    settings = get_settings()

    # 1) Validate the shared secret Telegram echoes back on every delivery.
    if settings.webhook_secret_token:
        if x_telegram_bot_api_secret_token != settings.webhook_secret_token:
            raise HTTPException(status_code=403, detail="invalid secret token")
    else:
        # Fail closed: refuse traffic if we never configured a secret.
        raise HTTPException(status_code=503, detail="webhook secret not configured")

    # 2) Parse update defensively (malformed bodies must not crash the bot).
    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid json body")
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return {"ok": True, "ignored": "no update_id"}

    # 3) Duplicate-update protection (Telegram retries deliveries).
    try:
        is_new = await state.db.mark_update_processed(update_id)
    except Exception as exc:  # noqa: BLE001
        log.error("dedupe check failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="storage unavailable")
    if not is_new:
        return {"ok": True, "ignored": "duplicate update"}

    # 4) Hand off to the bot logic. Errors are logged but we still return 200
    #    so Telegram does not endlessly retry a poisoned update.
    try:
        await state.handler.handle_update(update)
    except Exception as exc:  # noqa: BLE001
        log.exception("update handling failed: %s", type(exc).__name__)
        return {"ok": False, "error": "internal error"}
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
