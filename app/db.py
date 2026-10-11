"""PostgreSQL access layer (asyncpg).

All queries are parameterized and scoped by telegram_user_id so that every
user's history and memories stay isolated.  A thin ``InMemoryDB`` fallback with
the identical interface is included for unit tests and dry runs; production
always uses ``Database``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import asyncpg

log = logging.getLogger("companion.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id BIGINT PRIMARY KEY,
    display_name     TEXT,
    mode             TEXT NOT NULL DEFAULT 'friend',
    char_name        TEXT,
    personality      TEXT,
    tone             TEXT,
    pending_action   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id               BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content          TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_user_recent
    ON messages (telegram_user_id, id DESC);

CREATE TABLE IF NOT EXISTS memories (
    id               BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    content          TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (telegram_user_id, content)
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories (telegram_user_id, id DESC);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_id        BIGINT PRIMARY KEY,
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass
class UserState:
    telegram_user_id: int
    display_name: str | None = None
    mode: str = "friend"
    char_name: str | None = None
    personality: str | None = None
    tone: str | None = None
    pending_action: str | None = None


class Database:
    """asyncpg-backed storage."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, database_url: str, min_size: int = 1, max_size: int = 5) -> "Database":
        # Normalize common URL schemes so asyncpg accepts Render/Heroku-style DSNs.
        url = database_url.strip()
        for prefix in ("postgresql+asyncpg://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgres://" + url[len(prefix):]
                break
        pool = await asyncpg.create_pool(url, min_size=min_size, max_size=max_size)
        db = cls(pool)
        await db.init_schema()
        return db

    async def init_schema(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
        log.info("database schema initialized")

    async def close(self) -> None:
        await self.pool.close()

    async def healthcheck(self) -> bool:
        try:
            async with self.pool.acquire(timeout=3) as conn:
                val = await conn.fetchval("SELECT 1")
            return val == 1
        except Exception as exc:  # noqa: BLE001
            log.warning("db healthcheck failed: %s", type(exc).__name__)
            return False

    # --- users ---------------------------------------------------------------
    async def upsert_user(self, tg_id: int, display_name: str | None) -> UserState:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (telegram_user_id, display_name)
                VALUES ($1, $2)
                ON CONFLICT (telegram_user_id)
                DO UPDATE SET display_name = EXCLUDED.display_name, updated_at = now()
                RETURNING telegram_user_id, display_name, mode, char_name,
                          personality, tone, pending_action
                """,
                tg_id, display_name,
            )
        return _row_to_state(row)

    async def get_user(self, tg_id: int) -> UserState | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_user_id, display_name, mode, char_name, personality, tone, pending_action "
                "FROM users WHERE telegram_user_id = $1",
                tg_id,
            )
        return _row_to_state(row) if row else None

    async def set_mode(self, tg_id: int, mode: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET mode = $1, updated_at = now() WHERE telegram_user_id = $2",
                mode, tg_id,
            )

    async def set_profile(self, tg_id: int, char_name: str | None = None,
                          personality: str | None = None, tone: str | None = None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users SET
                    char_name   = COALESCE($2, char_name),
                    personality = COALESCE($3, personality),
                    tone        = COALESCE($4, tone),
                    updated_at  = now()
                WHERE telegram_user_id = $1
                """,
                tg_id, char_name, personality, tone,
            )

    async def set_pending(self, tg_id: int, action: str | None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET pending_action = $2, updated_at = now() WHERE telegram_user_id = $1",
                tg_id, action,
            )

    # --- messages ------------------------------------------------------------
    async def add_message(self, tg_id: int, role: str, content: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (telegram_user_id, role, content) VALUES ($1, $2, $3)",
                tg_id, role, content,
            )
            # Trim very old history beyond a generous cap to bound table size.
            await conn.execute(
                """
                DELETE FROM messages WHERE telegram_user_id = $1 AND id IN (
                    SELECT id FROM messages WHERE telegram_user_id = $1
                    ORDER BY id DESC OFFSET 500
                )
                """,
                tg_id,
            )

    async def get_history(self, tg_id: int, limit: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM messages WHERE telegram_user_id = $1 "
                "ORDER BY id DESC LIMIT $2",
                tg_id, limit,
            )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def clear_history(self, tg_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM messages WHERE telegram_user_id = $1", tg_id)

    # --- memories --------------------------------------------------------------
    async def add_memory(self, tg_id: int, content: str) -> bool:
        content = content.strip()[:500]
        if not content:
            return False
        async with self.pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE telegram_user_id = $1", tg_id
            )
            if existing >= 200:  # memory_limit_per_user default; enforced here too
                return False
            result = await conn.execute(
                "INSERT INTO memories (telegram_user_id, content) VALUES ($1, $2) "
                "ON CONFLICT (telegram_user_id, content) DO NOTHING",
                tg_id, content,
            )
        return result.endswith("1")

    async def get_memories(self, tg_id: int, limit: int = 30) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content FROM memories WHERE telegram_user_id = $1 "
                "ORDER BY id DESC LIMIT $2",
                tg_id, limit,
            )
        return [r["content"] for r in rows]

    async def search_memories(self, tg_id: int, query: str, limit: int = 10) -> list[str]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2][:6]
        if not words:
            return []
        conds = " OR ".join(f"content ILIKE '$${i + 2}'" for i in range(len(words)))
        sql = (
            f"SELECT content FROM memories WHERE telegram_user_id = $1 AND ({conds}) "
            f"ORDER BY id DESC LIMIT {limit}"
        )
        params = [tg_id] + [f"%{w}%" for w in words]
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [r["content"] for r in rows]

    async def delete_memories(self, tg_id: int) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM memories WHERE telegram_user_id = $1", tg_id)
        return int(result.split()[-1])

    # --- duplicate update protection -------------------------------------------
    async def mark_update_processed(self, update_id: int) -> bool:
        """Return True if this is a NEW update, False if it was already handled."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "INSERT INTO processed_updates (update_id) VALUES ($1) ON CONFLICT DO NOTHING",
                update_id,
            )
            if result.endswith("0"):
                return False
            # keep table small
            await conn.execute(
                "DELETE FROM processed_updates WHERE processed_at < now() - interval '7 days'"
            )
            return True


def _row_to_state(row) -> UserState:
    return UserState(
        telegram_user_id=row["telegram_user_id"],
        display_name=row["display_name"],
        mode=row["mode"],
        char_name=row["char_name"],
        personality=row["personality"],
        tone=row["tone"],
        pending_action=row["pending_action"],
    )


class InMemoryDB:
    """Drop-in fake used by unit tests; same interface, no PostgreSQL needed."""

    def __init__(self) -> None:
        self.users: dict[int, UserState] = {}
        self.messages: dict[int, list[dict]] = {}
        self.memories: dict[int, list[str]] = {}
        self.updates: set[int] = set()
        self._mem_counter = 0

    async def init_schema(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def healthcheck(self) -> bool:
        return True

    async def upsert_user(self, tg_id: int, display_name: str | None) -> UserState:
        st = self.users.get(tg_id)
        if st is None:
            st = UserState(telegram_user_id=tg_id, display_name=display_name)
            self.users[tg_id] = st
        else:
            st.display_name = display_name
        return st

    async def get_user(self, tg_id: int) -> UserState | None:
        return self.users.get(tg_id)

    async def set_mode(self, tg_id: int, mode: str) -> None:
        if tg_id in self.users:
            self.users[tg_id].mode = mode

    async def set_profile(self, tg_id: int, char_name: str | None = None,
                          personality: str | None = None, tone: str | None = None) -> None:
        st = self.users.get(tg_id)
        if not st:
            return
        if char_name is not None:
            st.char_name = char_name
        if personality is not None:
            st.personality = personality
        if tone is not None:
            st.tone = tone

    async def set_pending(self, tg_id: int, action: str | None) -> None:
        if tg_id in self.users:
            self.users[tg_id].pending_action = action

    async def add_message(self, tg_id: int, role: str, content: str) -> None:
        self.messages.setdefault(tg_id, []).append({"role": role, "content": content})

    async def get_history(self, tg_id: int, limit: int) -> list[dict]:
        return list(self.messages.get(tg_id, [])[-limit:])

    async def clear_history(self, tg_id: int) -> None:
        self.messages.pop(tg_id, None)

    async def add_memory(self, tg_id: int, content: str) -> bool:
        content = content.strip()[:500]
        if not content or content in self.memories.get(tg_id, []):
            return False
        if len(self.memories.get(tg_id, [])) >= 200:
            return False
        self.memories.setdefault(tg_id, []).append(content)
        return True

    async def get_memories(self, tg_id: int, limit: int = 30) -> list[str]:
        return list(reversed(self.memories.get(tg_id, [])))[:limit]

    async def search_memories(self, tg_id: int, query: str, limit: int = 10) -> list[str]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2][:6]
        out = []
        for m in reversed(self.memories.get(tg_id, [])):
            ml = m.lower()
            if any(w in ml for w in words):
                out.append(m)
            if len(out) >= limit:
                break
        return out

    async def delete_memories(self, tg_id: int) -> int:
        return len(self.memories.pop(tg_id, []))

    async def mark_update_processed(self, update_id: int) -> bool:
        if update_id in self.updates:
            return False
        self.updates.add(update_id)
        return True
