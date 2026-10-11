"""Conversation orchestration: history + memories -> LLM reply -> persistence."""

from __future__ import annotations

import logging

from app.characters import build_system_prompt, is_valid_mode
from app.llm import LLMAuthError, LLMError, LLMRateLimitError, LLMResponseError, LLMTimeoutError
from app.memory import extract_memories_llm, extract_memories_rule_based, rank_memories

log = logging.getLogger("companion.chat")

FALLBACK_REPLIES = {
    "timeout": "Hmm, my thoughts got lost for a second 😅 try again?",
    "rate_limit": "I'm getting a lot of messages right now — give me a minute and ping me again 💬",
    "error": "Ugh, something glitched on my side. Say that again? 🙈",
}


class ChatOrchestrator:
    def __init__(self, db, llm, settings) -> None:
        self.db = db
        self.llm = llm
        self.settings = settings

    async def respond(self, tg_id: int, user_text: str) -> str:
        """Produce one in-character reply for a user message, persisting state."""
        state = await self.db.get_user(tg_id)
        if state is None:  # defensive: webhook layer always upserts first
            state = await self.db.upsert_user(tg_id, None)

        memories = await self.db.search_memories(tg_id, user_text, limit=10)
        if not memories:
            memories = await self.db.get_memories(tg_id, limit=15)
        else:
            extra = await self.db.get_memories(tg_id, limit=15)
            merged = memories + [m for m in extra if m not in memories]
            memories = rank_memories(merged, user_text, limit=12)

        system_prompt = build_system_prompt(
            mode=state.mode if is_valid_mode(state.mode) else "friend",
            char_name=state.char_name,
            personality=state.personality,
            tone=state.tone,
            user_name=state.display_name,
            memories=memories,
        )

        history = await self.db.get_history(tg_id, self.settings.history_window)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        try:
            reply = await self.llm.chat(messages)
        except LLMTimeoutError:
            log.warning("llm timeout for user %s", tg_id)
            return FALLBACK_REPLIES["timeout"]
        except LLMRateLimitError:
            log.warning("llm rate limited for user %s", tg_id)
            return FALLBACK_REPLIES["rate_limit"]
        except LLMAuthError:
            log.error("llm auth error -- check LLM_API_KEY (user %s)", tg_id)
            return FALLBACK_REPLIES["error"]
        except (LLMResponseError, LLMError) as exc:
            log.error("llm failure for user %s: %s", tg_id, type(exc).__name__)
            return FALLBACK_REPLIES["error"]

        # Persist both sides of the turn (history survives restarts).
        await self.db.add_message(tg_id, "user", user_text)
        await self.db.add_message(tg_id, "assistant", reply)

        # Long-term memory: cheap rules first; never let extraction break chat.
        new_memories = extract_memories_rule_based(user_text)
        saved = 0
        for memo in new_memories:
            if await self.db.add_memory(tg_id, memo):
                saved += 1
        if saved == 0:
            llm_memories = await extract_memories_llm(self.llm, user_text, reply)
            for memo in llm_memories:
                await self.db.add_memory(tg_id, memo)

        return reply
