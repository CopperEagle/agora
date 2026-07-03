"""Tests for agent-optimized tool descriptions.

Verifies that all 9 tool descriptions (5 backbone + 4 chat) follow the
agent-actionable template: ``<action> <what>. Use this when <scenario>.``

Key properties:
- Contains actionable trigger phrase ("Use this when" or "Call this")
- Under 30 words for context efficiency
- Includes discoverability keywords and side-effect/constraint visibility
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from agora.backbone.server import AgoraServer

# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def server() -> AsyncGenerator[AgoraServer, None]:
    """Provide a started AgoraServer with backbone + Chat plugin."""
    cfg: dict[str, object] = {
        "db_path": ":memory:",
        "plugins": [
            {
                "name": "chat",
                "enabled": True,
                "module": "agora.plugins.chat",
                "class_name": "ChatPlugin",
                "config": {},
            },
        ],
    }
    srv = AgoraServer(config=cfg, skip_transport=True)
    await srv.start()
    yield srv
    await srv.stop()


def _get_descriptions(server: AgoraServer) -> dict[str, str]:
    """Return tool_name → description from the router's metadata."""
    assert server._router is not None  # noqa: SLF001
    meta = server._router.list_tool_metadata()
    return {m["name"]: m["description"] for m in meta}


def _word_count(text: str) -> int:
    """Count whitespace-separated words in text."""
    return len(text.split())


# ── Tests ───────────────────────────────────────────────────────


async def test_all_descriptions_are_agent_actionable(server: AgoraServer) -> None:
    """Every description must contain 'Use this when' or 'Call this'."""
    descs = _get_descriptions(server)
    for name, desc in descs.items():
        assert desc, f"{name} has empty description"
        has_trigger = "Use this when" in desc or "Call this" in desc
        assert has_trigger, (
            f"{name} description lacks agent-actionable trigger phrase: {desc!r}"
        )


async def test_no_description_exceeds_30_words(server: AgoraServer) -> None:
    """Every description must be ≤ 30 words for context efficiency."""
    descs = _get_descriptions(server)
    for name, desc in descs.items():
        wc = _word_count(desc)
        assert wc <= 30, (
            f"{name} description is {wc} words (max 30): {desc!r}"
        )


async def test_chat_list_channels_mentions_prefix(server: AgoraServer) -> None:
    """chat_list_channels description must mention 'prefix' for discoverability."""
    descs = _get_descriptions(server)
    desc = descs["chat_list_channels"]
    assert "prefix" in desc.lower(), (
        f"chat_list_channels description must mention 'prefix': {desc!r}"
    )


async def test_chat_post_message_mentions_auto_creates(server: AgoraServer) -> None:
    """chat_post_message must disclose auto-create side effect."""
    descs = _get_descriptions(server)
    desc = descs["chat_post_message"]
    assert "Auto-creates" in desc or "auto-creates" in desc, (
        f"chat_post_message must mention 'Auto-creates': {desc!r}"
    )


async def test_chat_post_message_mentions_max_constraint(server: AgoraServer) -> None:
    """chat_post_message must state the 100,000 char constraint."""
    descs = _get_descriptions(server)
    desc = descs["chat_post_message"]
    assert "100,000" in desc, (
        f"chat_post_message must mention '100,000': {desc!r}"
    )


async def test_register_mentions_first(server: AgoraServer) -> None:
    """register description must mention 'first' for ordering guidance."""
    descs = _get_descriptions(server)
    desc = descs["register"]
    assert "first" in desc.lower(), (
        f"register description must mention 'first': {desc!r}"
    )
