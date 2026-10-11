"""End-to-end handler flow tests: onboarding, chat, commands, resilience."""

from app.chat import FALLBACK_REPLIES
from app.llm import LLMRateLimitError, LLMTimeoutError
from tests.conftest import FakeLLM, make_update


async def test_start_onboarding_then_chat(handler, db, fake_tg, fake_llm):
    # /start -> pending onboarding
    await handler.handle_update(make_update(1, "/start"))
    assert db.users[111].pending_action == "onboarding"

    # invalid mode keeps asking
    await handler.handle_update(make_update(2, "enemy"))
    assert db.users[111].pending_action == "onboarding"

    # valid mode completes onboarding
    await handler.handle_update(make_update(3, "girlfriend"))
    assert db.users[111].pending_action is None
    assert db.users[111].mode == "girlfriend"

    # plain chat now hits the orchestrator
    await handler.handle_update(make_update(4, "hi there"))
    assert any("stub" not in t for _, t in fake_tg.sent)  # got a reply
    assert fake_llm.calls, "LLM should have been invoked"
    # system prompt must carry the girlfriend character name
    assert "Mia" in fake_llm.calls[-1][0]["content"]


async def test_mode_switch_persists(handler, db):
    await handler.handle_update(make_update(1, "/mode boyfriend"))
    assert db.users[111].mode == "boyfriend"
    await handler.handle_update(make_update(2, "/mode bogus"))
    assert db.users[111].mode == "boyfriend", "invalid mode must not change state"


async def test_personality_customization(handler, db, fake_llm):
    await handler.handle_update(make_update(1, "/personality name Valentina"))
    assert db.users[111].char_name == "Valentina"
    await handler.handle_update(make_update(2, "/personality traits shy but witty"))
    assert db.users[111].personality == "shy but witty"
    await handler.handle_update(make_update(3, "/personality tone soft"))
    assert db.users[111].tone == "soft"

    await handler.handle_update(make_update(4, "hello"))
    sys_prompt = fake_llm.calls[-1][0]["content"]
    assert "Valentina" in sys_prompt and "shy but witty" in sys_prompt and "soft" in sys_prompt


async def test_memory_commands_roundtrip(handler, db, fake_tg):
    await handler.handle_update(make_update(1, "/memory"))
    assert "Nothing saved" in fake_tg.last()

    await db.upsert_user(111, "Sam")
    await db.add_memory(111, "User's name is Sam")
    await handler.handle_update(make_update(2, "/memory"))
    assert "User's name is Sam" in fake_tg.last()

    await handler.handle_update(make_update(3, "/forget"))
    assert await db.get_memories(111) == []


async def test_reset_requires_confirmation(handler, db):
    await db.upsert_user(111, "Sam")
    await db.add_message(111, "user", "some history")
    await handler.handle_update(make_update(1, "/reset"))
    assert db.users[111].pending_action == "confirm_reset"
    assert len(await db.get_history(111, 50)) == 1, "not cleared before confirm"

    # cancel path
    await handler.handle_update(make_update(2, "no"))
    assert db.users[111].pending_action is None
    assert len(await db.get_history(111, 50)) == 1

    # confirm path
    await handler.handle_update(make_update(3, "/reset"))
    await handler.handle_update(make_update(4, "yes"))
    assert await db.get_history(111, 50) == []


async def test_conversation_history_and_memories_persist(handler, db, fake_llm):
    await handler.handle_update(make_update(1, "/mode friend"))
    await handler.handle_update(make_update(2, "My name is Robin. I love hiking."))
    hist = await db.get_history(111, 50)
    assert hist[0]["role"] == "user" and hist[1]["role"] == "assistant"
    mems = " | ".join(await db.get_memories(111)).lower()
    assert "robin" in mems and "hiking" in mems

    # second turn includes previous history in the payload (multi-turn).
    # FakeLLM returns a canned reply, so the assistant turn carries no name;
    # the user's earlier message must appear as history before this turn.
    await handler.handle_update(make_update(3, "how are you doing?"))
    last_call = fake_llm.calls[-1]
    roles = [m["role"] for m in last_call]
    assert roles[0] == "system"
    assert roles[-1] == "user" and last_call[-1]["content"] == "how are you doing?"
    assert {"user", "assistant"} <= set(roles), "history should include past turns"
    sys_prompt = last_call[0]["content"].lower()
    assert "robin" in sys_prompt, "saved memory must reach the system prompt"


async def test_llm_timeout_returns_in_character_fallback(handler, fake_tg):
    handler.chat.llm.error = LLMTimeoutError("boom")
    await handler.handle_update(make_update(1, "/mode friend"))
    await handler.handle_update(make_update(2, "hey"))
    assert fake_tg.last() == FALLBACK_REPLIES["timeout"]


async def test_llm_rate_limit_fallback(handler, fake_tg):
    handler.chat.llm.error = LLMRateLimitError("slow down")
    await handler.handle_update(make_update(1, "/mode friend"))
    await handler.handle_update(make_update(2, "hey"))
    assert fake_tg.last() == FALLBACK_REPLIES["rate_limit"]


async def test_rate_limiter_blocks_burst(handler, settings):
    await handler.handle_update(make_update(0, "/mode friend"))
    n = settings.rate_limit_messages
    for i in range(n + 5):
        await handler.handle_update(make_update(100 + i, f"spam {i}"))
    throttled = sum(1 for _, t in handler.tg.sent if "Slow down" in t)
    assert throttled >= 1, "burst must be rate limited"


async def test_oversized_message_rejected(handler, fake_tg, settings):
    await handler.handle_update(make_update(1, "x" * (settings.max_message_length + 1)))
    assert "novel" in fake_tg.last()


async def test_unknown_command_guides_to_help(handler, fake_tg):
    await handler.handle_update(make_update(1, "/frobnicate"))
    assert "/help" in fake_tg.last()
