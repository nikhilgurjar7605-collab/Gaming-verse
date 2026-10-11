"""Long-term memory extraction and retrieval.

Two layers:
1. Fast, deterministic rule-based extraction (zero LLM cost) that catches
   explicit self-disclosures ("my name is ...", "I work as ...", preferences).
2. Optional lightweight LLM extraction for free-form facts, run in the
   background with a tiny prompt and strict output parsing.

Memories are always stored per Telegram user id.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("companion.memory")

# --------------------------------------------------------------------------
# Rule-based extractors: (regex, template) pairs applied to user messages.
# --------------------------------------------------------------------------
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:my name is|i'?m called|call me|this is)\s+([A-Za-z][A-Za-z\-]{1,30})", re.I),
     "User's name is {0}"),
    (re.compile(r"\bi(?:'m|\s+am)\s+(\d{1,2})\s*(?:years?\s*old|yo)\b", re.I),
     "User is {0} years old"),
    (re.compile(r"\bi\s+(?:work(?:ing)?|study)\s+(?:as|at|in|for)?\s*(.{3,60}?)(?=[.!?,]|$)", re.I),
     "User works/studies as {0}"),
    (re.compile(r"\bi\s+(?:love|adore|really like|enjoy)\s+(.{3,60}?)(?=[.!?,]|$)", re.I),
     "User loves {0}"),
    (re.compile(r"\bi\s+(?:hate|dislike|can'?t stand)\s+(.{3,60}?)(?=[.!?,]|$)", re.I),
     "User dislikes {0}"),
    (re.compile(r"\bmy\s+(birthday|anniversary)\s+is\s+(.{3,40}?)(?=[.!?,]|$)", re.I),
     "User's {0} is {1}"),
    (re.compile(r"\bi\s+(?:live|grew up)\s+(?:in|at)\s+(.{3,40}?)(?=[.!?,]|$)", re.I),
     "User lives in {0}"),
    (re.compile(r"\b(?:i have|i'?ve got)\s+(a |an )?(dog|cat|pet|parrot|hamster)\b", re.I),
     "User has a {1}"),
]

_STOPWORD_REPLIES = {"you", "you too", "same", "lol"}


def extract_memories_rule_based(text: str) -> list[str]:
    """Return zero or more short memory strings from one user message."""
    out: list[str] = []
    for pattern, template in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groups()
        try:
            memo = template.format(*(g.strip() if isinstance(g, str) else g for g in groups))
        except (IndexError, KeyError):
            continue
        # clean trailing punctuation & collapse whitespace
        memo = re.sub(r"\s+", " ", memo).strip(" .,!")
        low = memo.lower()
        if low in _STOPWORD_REPLIES or len(memo) < 8:
            continue
        # avoid capturing roleplay lines like "*smiles*"
        if "*" in memo or "@" in memo:
            continue
        out.append(memo[:200])
    return out[:3]  # keep at most 3 memories per message


_MEMORY_EXTRACT_PROMPT = """\
Extract durable personal facts about the USER from the two messages below.
Rules:
- Only facts the USER stated about themselves (name, age, job, likes, dislikes,
  pets, location, important dates, ongoing projects/people in their life).
- Each fact on its own line, max 3 lines, <= 100 chars each, phrased as a short
  third-person note ("User prefers tea over coffee").
- No guesses, no inferences from mood, nothing about the assistant character.
- If there is nothing worth remembering, reply with exactly: NONE
"""


async def extract_memories_llm(llm, user_text: str, assistant_text: str) -> list[str]:
    """Optional second-pass extraction using the main model (cheap call)."""
    if len(user_text) < 12:
        return []
    try:
        raw = await llm.chat(
            [
                {"role": "system", "content": _MEMORY_EXTRACT_PROMPT},
                {"role": "user", "content": f"USER: {user_text[:1500]}\nASSISTANT: {assistant_text[:800]}"},
            ],
            max_tokens=150,
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001 -- memory extraction must never break chat
        log.info("llm memory extraction skipped: %s", type(exc).__name__)
        return []
    if not raw or raw.strip().upper().startswith("NONE"):
        return []
    lines = [l.strip(" -•*\t") for l in raw.splitlines() if l.strip()]
    return [l[:200] for l in lines if 8 <= len(l) <= 200][:3]


def rank_memories(all_memories: list[str], query: str, limit: int = 10) -> list[str]:
    """Very small relevance ranking for retrieval into the system prompt."""
    stop = {"what", "when", "where", "which", "about", "know", "tell", "your",
            "youre", "with", "that", "this", "have", "will", "would", "could"}
    words = {w.lower() for w in re.findall(r"\w+", query) if len(w) > 2} - stop
    if not words:
        return all_memories[:limit]
    scored = []
    for mem in all_memories:
        ml = mem.lower()
        score = sum(1 for w in words if w in ml)
        scored.append((-score, mem))
    scored.sort()
    hits = [m for s, m in scored if -s > 0]
    rest = [m for s, m in scored if -s == 0]
    return (hits + rest)[:limit]
