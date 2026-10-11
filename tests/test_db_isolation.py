"""Database-layer tests: per-user isolation of history & memories, dedupe, caps."""

from app.memory import extract_memories_rule_based, rank_memories


async def test_user_isolation_messages(db):
    await db.upsert_user(1001, "Alice")
    await db.upsert_user(2002, "Bob")
    await db.add_message(1001, "user", "secret alice thing")
    await db.add_message(2002, "user", "secret bob thing")

    h1 = await db.get_history(1001, limit=50)
    h2 = await db.get_history(2002, limit=50)
    assert [m["content"] for m in h1] == ["secret alice thing"]
    assert [m["content"] for m in h2] == ["secret bob thing"]

    await db.clear_history(1001)
    assert await db.get_history(1001, limit=50) == []
    assert len(await db.get_history(2002, limit=50)) == 1, "clear must be scoped"


async def test_user_isolation_memories(db):
    await db.upsert_user(1001, "Alice")
    await db.upsert_user(2002, "Bob")
    assert await db.add_memory(1001, "User's name is Alice")
    assert await db.add_memory(2002, "User has a cat")

    assert await db.get_memories(1001) == ["User's name is Alice"]
    assert await db.search_memories(2002, "tell me about dogs and cats") == ["User has a cat"]
    assert await db.search_memories(1001, "tell me about cats") == [], "no cross-user reads"

    deleted = await db.delete_memories(2002)
    assert deleted == 1
    assert await db.get_memories(1001), "forget must be scoped"


async def test_duplicate_memory_ignored_and_cap(db):
    await db.upsert_user(1001, "A")
    assert await db.add_memory(1001, "User likes pizza")
    assert not await db.add_memory(1001, "User likes pizza"), "dedupe by content"
    for i in range(200):
        await db.add_memory(1001, f"fact number {i} about user")
    mems = await db.get_memories(1001, limit=500)
    assert len(mems) <= 200, "memory cap enforced"


async def test_update_dedupe(db):
    assert await db.mark_update_processed(42) is True
    assert await db.mark_update_processed(42) is False
    assert await db.mark_update_processed(43) is True


async def test_profile_and_mode_persistence(db):
    await db.upsert_user(1001, "A")
    await db.set_mode(1001, "girlfriend")
    await db.set_profile(1001, char_name="Luna", tone="playful")
    st = await db.get_user(1001)
    assert st.mode == "girlfriend" and st.char_name == "Luna" and st.tone == "playful"
    # COALESCE-style partial update keeps untouched fields
    await db.set_profile(1001, personality="shy")
    st = await db.get_user(1001)
    assert st.char_name == "Luna" and st.personality == "shy"


def test_rule_based_extraction():
    mems = extract_memories_rule_based("My name is Leo. I love ramen and I am 24 years old.")
    joined = " | ".join(mems).lower()
    assert "name is leo" in joined
    assert "ramen" in joined
    assert "24 years old" in joined
    assert extract_memories_rule_based("hi there!") == []


def test_rank_memories_prefers_relevant():
    mems = ["User likes sushi", "User has a sister named May", "User lives in Osaka"]
    top = rank_memories(mems, "what do you know about my sister?", limit=3)
    assert top[0] == "User has a sister named May"
