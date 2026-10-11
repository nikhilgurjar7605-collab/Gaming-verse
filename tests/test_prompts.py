"""Tests for character prompts and system-prompt assembly."""

from app.characters import (
    CHARACTER_PROMPTS,
    DEFAULT_NAMES,
    build_system_prompt,
    is_valid_mode,
)


def test_three_distinct_modes_exist():
    assert set(CHARACTER_PROMPTS) == {"friend", "girlfriend", "boyfriend"}
    prompts = list(CHARACTER_PROMPTS.values())
    assert len(set(prompts)) == 3, "each mode must have a distinct prompt"


def test_default_names_differ_per_mode():
    assert len(set(DEFAULT_NAMES.values())) == 3


def test_invalid_mode_rejected():
    assert not is_valid_mode("enemy")
    assert is_valid_mode("friend")


def test_girlfriend_prompt_is_romantic_friend_prompt_is_not():
    gf = build_system_prompt("girlfriend").lower()
    fr = build_system_prompt("friend").lower()
    assert "girlfriend" in gf
    assert "not romantic" in fr


def test_build_prompt_includes_customization():
    p = build_system_prompt(
        "boyfriend", char_name="Rex", personality="goofy", tone="dry",
        user_name="Ana", memories=["User loves jazz"],
    )
    assert "Rex" in p and "goofy" in p and "dry" in p
    assert "Ana" in p and "User loves jazz" in p
    # default name must NOT leak when overridden
    assert DEFAULT_NAMES["boyfriend"] not in p


def test_no_unresolved_placeholders():
    for mode in CHARACTER_PROMPTS:
        p = build_system_prompt(mode)
        assert "{name}" not in p and "{SHARED_RULES}" not in p


def test_guardrails_present_in_every_mode():
    for mode, _ in CHARACTER_PROMPTS.items():
        p = build_system_prompt(mode).lower()
        assert "as an ai" in p          # anti-assistant-speak rule
        assert "never claim to be a real human" in p
        assert "consent" in p or "consenting" in p
