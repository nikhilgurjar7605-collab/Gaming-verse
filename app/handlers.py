"""Telegram update routing: commands, onboarding flow and chat handling.

Kept framework-agnostic (plain dicts in / injected sender) so it is easy to
unit test without FastAPI or a live Telegram connection.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from app.characters import DEFAULT_NAMES, MODES, is_valid_mode
from app.chat import ChatOrchestrator

log = logging.getLogger("companion.handlers")

HELP_TEXT = (
    "🤖 ai-companion-bot commands\n\n"
    "/start – pick your companion & get started\n"
    "/mode – switch relationship style (friend / girlfriend / boyfriend)\n"
    "/personality – customize name, traits and tone\n"
    "/memory – see what I remember about you\n"
    "/forget – delete all saved memories\n"
    "/reset – clear our conversation history (with confirmation)\n"
    "/help – this list\n\n"
    "Just text me anything after /start — I'll reply in character 💬"
)

CONFIRM_WORDS = {"yes", "y", "confirm", "ok", "do it"}
CANCEL_WORDS = {"no", "n", "cancel", "stop"}


class RateLimiter:
    """Simple sliding-window limiter per user (in-process)."""

    def __init__(self, max_messages: int, window_seconds: int) -> None:
        self.max = max_messages
        self.window = window_seconds
        self.hits: dict[int, deque] = defaultdict(deque)

    def allow(self, tg_id: int) -> bool:
        now = time.monotonic()
        q = self.hits[tg_id]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return False
        q.append(now)
        return True


class BotHandler:
    def __init__(self, db, orchestrator: ChatOrchestrator, tg_client, settings) -> None:
        self.db = db
        self.chat = orchestrator
        self.tg = tg_client          # any object with async send_message(chat_id, text)
        self.settings = settings
        self.limiter = RateLimiter(settings.rate_limit_messages,
                                   settings.rate_limit_window_seconds)

    # ------------------------------------------------------------------ entry
    async def handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message or "text" not in message:
            return  # MVP: ignore edits/photos/callbacks
        from_user = message.get("from") or {}
        tg_id = from_user.get("id")
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not tg_id or chat_id is None or not text:
            return

        first_name = from_user.get("first_name")

        if len(text) > self.settings.max_message_length:
            await self.tg.send_message(
                chat_id,
                f"Whoa, that's a novel 😄 keep messages under {self.settings.max_message_length} characters.")
            return

        # Duplicate protection happens at the webhook layer via update_id.
        if text.startswith("/"):
            await self._handle_command(tg_id, chat_id, first_name, text)
        else:
            if not self.limiter.allow(tg_id):
                await self.tg.send_message(
                    chat_id,
                    "Slow down a sec, I'm juggling too many messages 😅 try again in a minute.")
                return
            state = await self.db.upsert_user(tg_id, first_name)
            if state.pending_action == "onboarding":
                await self._finish_onboarding(tg_id, chat_id, text)
            elif state.pending_action == "confirm_reset":
                await self._resolve_reset_confirm(tg_id, chat_id, text)
            else:
                reply = await self.chat.respond(tg_id, text)
                await self.tg.send_message(chat_id, reply)

    # ------------------------------------------------------ reset confirmation
    async def _resolve_reset_confirm(self, tg_id: int, chat_id: int, text: str) -> None:
        word = text.lower().strip(" .!¡!")
        await self.db.set_pending(tg_id, None)
        if word in CONFIRM_WORDS:
            await self.db.clear_history(tg_id)
            await self.tg.send_message(
                chat_id, "History cleared 🧻 fresh start — what's on your mind?")
        elif word in CANCEL_WORDS:
            await self.tg.send_message(chat_id, "Okay, keeping our history then 😌")
        else:
            await self.tg.send_message(
                chat_id, 'Reply *yes* to confirm the reset or *no* to cancel 🙂')

    # --------------------------------------------------------------- commands
    async def _handle_command(self, tg_id: int, chat_id: int, first_name: str | None,
                              text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/start":
            await self.db.upsert_user(tg_id, first_name)
            await self.db.set_pending(tg_id, "onboarding")
            greeting = f"Hey {first_name}! 👋 " if first_name else "Hey there! 👋 "
            await self.tg.send_message(
                chat_id,
                greeting +
                "I'm your AI companion. Pick how we should relate — reply with one of:\n\n"
                "🫶 *friend* — witty, loyal bestie energy\n"
                "💜 *girlfriend* — affectionate, flirty, always checks in\n"
                "💙 *boyfriend* — warm, teasing, cheesy-romantic\n\n"
                "You can change this anytime with /mode, and shape my vibe with /personality.")
            return

        if cmd == "/mode":
            mode = arg.lower()
            if not mode:
                buttons = "\n".join(f"• /mode {m}" for m in MODES)
                await self.tg.send_message(chat_id, "Which style? Reply with:\n" + buttons)
                return
            if not is_valid_mode(mode):
                await self.tg.send_message(
                    chat_id, f"Unknown mode '{mode}'. Choose: friend, girlfriend, boyfriend.")
                return
            await self.db.upsert_user(tg_id, first_name)
            await self.db.set_mode(tg_id, mode)
            await self.db.set_pending(tg_id, None)
            await self.tg.send_message(
                chat_id,
                f"Switched to *{mode}* mode ✨ I'm {DEFAULT_NAMES[mode]} now — hey, good to see you!")
            return

        if cmd == "/personality":
            await self.db.upsert_user(tg_id, first_name)
            st = await self.db.get_user(tg_id)
            if not arg:
                await self.tg.send_message(
                    chat_id,
                    "Customize me! Reply with one of:\n\n"
                    f"• `/personality name <new name>` (now: {st.char_name if st and st.char_name else 'default'})\n"
                    f"• `/personality traits <description>` (now: {st.personality if st and st.personality else 'default'})\n"
                    f"• `/personality tone <description>` (now: {st.tone if st and st.tone else 'default'})\n\n"
                    "Example: `/personality traits sarcastic but soft-hearted, loves astronomy`")
                return
            low = arg.lower()
            if low.startswith(("name ", "name=")):
                value = (arg.split(None, 1)[1] if " " in arg else arg[5:]).strip()
                if not value or len(value) > 40:
                    await self.tg.send_message(chat_id, "Name must be 1–40 characters 🙂")
                    return
                await self.db.set_profile(tg_id, char_name=value)
                await self.tg.send_message(chat_id, f"Done! Call me *{value}* now 😌")
            elif low.startswith(("traits ", "traits=", "personality ", "personality=")):
                sep = " " if " " in arg[:13] else "="
                value = arg.split(sep, 1)[1].strip()[:300]
                await self.db.set_profile(tg_id, personality=value)
                await self.tg.send_message(
                    chat_id, "Personality updated — notice a difference already? 😏")
            elif low.startswith(("tone ", "tone=")):
                sep = " " if " " in arg[:6] else "="
                value = arg.split(sep, 1)[1].strip()[:200]
                await self.db.set_profile(tg_id, tone=value)
                await self.tg.send_message(
                    chat_id, f"Tone set: *{value}*. Say something and see 😉")
            else:
                await self.tg.send_message(
                    chat_id,
                    "Use `/personality name <name>`, `/personality traits <traits>` "
                    "or `/personality tone <tone>`.")
            return

        if cmd == "/memory":
            mems = await self.db.get_memories(tg_id, limit=25)
            if not mems:
                await self.tg.send_message(
                    chat_id, "Nothing saved yet — tell me about yourself and I'll remember 💾")
                return
            lines = "\n".join(f"• {m}" for m in mems)
            await self.tg.send_message(
                chat_id, f"Here's what I remember about you ({len(mems)}):\n{lines}")
            return

        if cmd == "/forget":
            deleted = await self.db.delete_memories(tg_id)
            await self.tg.send_message(
                chat_id,
                f"Wiped {deleted} memories 🧹 clean slate — make some new ones?" if deleted
                else "I didn't have anything saved anyway 🤷")
            return

        if cmd == "/reset":
            await self.db.upsert_user(tg_id, first_name)
            await self.db.set_pending(tg_id, "confirm_reset")
            await self.tg.send_message(
                chat_id,
                "⚠️ This deletes our conversation history (memories are kept). "
                "Reply *yes* to confirm or *no* to cancel.")
            return

        if cmd == "/help":
            await self.tg.send_message(chat_id, HELP_TEXT)
            return

        if cmd == "/cancel":
            await self.db.set_pending(tg_id, None)
            await self.tg.send_message(chat_id, "Okay, never mind 😊")
            return

        await self.tg.send_message(chat_id, "Unknown command. Try /help 📖")

    # ------------------------------------------------------ onboarding helper
    async def _finish_onboarding(self, tg_id: int, chat_id: int, text: str) -> None:
        mode = text.lower().strip()
        if mode in ("/skip", "skip"):
            mode = "friend"
        if not is_valid_mode(mode):
            await self.tg.send_message(
                chat_id, "Pick one of: friend, girlfriend, boyfriend 🙂 (or /skip for friend)")
            return
        await self.db.set_mode(tg_id, mode)
        await self.db.set_pending(tg_id, None)
        name = DEFAULT_NAMES[mode]
        await self.tg.send_message(
            chat_id,
            f"Yay, let's do *{mode}* — I'm {name} 💫\n\n"
            "Tell me something about yourself (your name, what you did today, what you love) "
            "— I'll remember it. Want a different vibe? Use /personality anytime.")
