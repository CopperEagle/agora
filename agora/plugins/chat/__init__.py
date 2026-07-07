"""Chat plugin — shared chatrooms for agent coordination.

Provides tools for posting messages, reading channel history, listing
channels, and summarizing conversations. Channels auto-vivify on first
post.  Messages are append-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

import apsw

from agora.backbone import AgoraPlugin, ToolDef

from .migrations import get_migrations

logger = logging.getLogger(__name__)

_DEFAULT_MAX_MESSAGE_LENGTH = 100_000
_DEFAULT_MAX_CHANNELS = 1000
_MAX_WAITERS_PER_CHANNEL = 100
_SYSTEM_AGENT_ID = "system"
_CHANNEL_GENERAL = "#general"


class ChatPlugin(AgoraPlugin):
    """Shared chatrooms for agent coordination.

    Agents post messages to channels, read message history, list available
    channels, and request channel summaries.  Every tool is prefixed with
    ``chat_`` by the backbone.

    Attributes:
        max_message_length: Maximum characters per message.
        max_channels: Maximum number of channels allowed.
        llm_api_url: Optional LLM endpoint for summarization.
        llm_api_key: API key for the LLM endpoint.
        llm_model: Model name for the LLM.
        llm_system_prompt: System prompt for summarization.
        llm_max_tokens: Max tokens for the LLM response.
        use_built_in_llm: Whether to use the built-in (free) LLM.
        _channel_names: Set of known channel names (cache).
    """

    name = "chat"
    version = "1.0.0"
    description = "Shared chatrooms for agent coordination"

    def __init__(self) -> None:
        """Initialize the chat plugin state."""
        super().__init__()
        self.max_message_length: int = _DEFAULT_MAX_MESSAGE_LENGTH
        self.max_channels: int = _DEFAULT_MAX_CHANNELS
        self.llm_api_url: str = ""
        self.llm_api_key: str = ""
        self.llm_model: str = "gpt-4o-mini"
        self.llm_system_prompt: str = (
            "Summarize the following chat messages concisely."
        )
        self.llm_max_tokens: int = 500
        self.use_built_in_llm: bool = False
        self._channel_names: set[str] = set()
        self._channel_lock: asyncio.Lock = asyncio.Lock()
        self._waiters: defaultdict[str, list[asyncio.Event]] = defaultdict(list)

    # ── Lifecycle hooks ──────────────────────────────────────────

    async def on_load(self, config: dict[str, object]) -> None:
        """Parse the plugin configuration.

        Args:
            config: Plugin-specific config dict from the server config.
        """
        raw_max_len = config.get("max_message_length", _DEFAULT_MAX_MESSAGE_LENGTH)
        self.max_message_length = int(str(raw_max_len))
        raw_max_chan = config.get("max_channels", _DEFAULT_MAX_CHANNELS)
        self.max_channels = int(str(raw_max_chan))
        self.llm_api_url = str(config.get("llm_api_url", ""))
        self.llm_api_key = str(config.get("llm_api_key", ""))
        self.llm_model = str(config.get("llm_model", "gpt-4o-mini"))
        self.llm_system_prompt = str(
            config.get(
                "llm_system_prompt",
                "Summarize the following chat messages concisely.",
            ),
        )
        raw_tokens = config.get("llm_max_tokens", 500)
        self.llm_max_tokens = int(str(raw_tokens))
        self.use_built_in_llm = bool(config.get("use_built_in_llm", False))

    async def on_startup(self) -> None:
        """Initialize caches and subscribe to agent lifecycle events."""
        # Pre-warm the channel name cache from the database
        await self._refresh_channel_cache()
        # Subscribe to agent lifecycle events
        if self.eventbus is not None:
            self.eventbus.subscribe(
                "agent.registered", self._on_agent_registered_event,
            )
            self.eventbus.subscribe(
                "agent.disconnected", self._on_agent_disconnected_event,
            )
            self.eventbus.subscribe(
                "chat.message.posted", self._on_message_posted,
            )

    async def on_shutdown(self) -> None:
        """Cleanup — wake all waiters and clear caches."""
        if self.eventbus is not None:
            self.eventbus.unsubscribe(
                "chat.message.posted", self._on_message_posted,
            )
            self.eventbus.unsubscribe(
                "agent.registered", self._on_agent_registered_event,
            )
            self.eventbus.unsubscribe(
                "agent.disconnected", self._on_agent_disconnected_event,
            )
        for channel_waiters in self._waiters.values():
            for event in channel_waiters:
                try:
                    event.set()
                except Exception:
                    logger.exception("Failed to wake waiter during shutdown")
        self._waiters.clear()
        self._channel_names.clear()

    # ── Agent lifecycle hooks ─────────────────────────────────────

    async def on_agent_register(self, agent_id: str) -> None:
        """Post a 'joined' message to the #general channel.

        Args:
            agent_id: The UUID of the registered agent.
        """
        await self._post_system_message(
            _CHANNEL_GENERAL, f"Agent `{agent_id}` joined",
        )

    async def on_agent_disconnect(self, agent_id: str) -> None:
        """Post a 'left' message to the #general channel.

        Args:
            agent_id: The UUID of the disconnected agent.
        """
        await self._post_system_message(
            _CHANNEL_GENERAL, f"Agent `{agent_id}` left",
        )

    async def _on_agent_registered_event(
        self, event_name: str, **data: object,
    ) -> None:
        """Handle agent.registered events from the event bus.

        Args:
            event_name: The event name (unused).
            **data: Event payload containing ``agent_id``.
        """
        _ = event_name
        agent_id = str(data.get("agent_id", ""))
        if agent_id:
            await self.on_agent_register(agent_id)

    async def _on_agent_disconnected_event(
        self, event_name: str, **data: object,
    ) -> None:
        """Handle agent.disconnected events from the event bus.

        Args:
            event_name: The event name (unused).
            **data: Event payload containing ``agent_id``.
        """
        _ = event_name
        agent_id = str(data.get("agent_id", ""))
        if agent_id:
            await self.on_agent_disconnect(agent_id)

    async def _on_message_posted(
        self, event_name: str, **data: object,
    ) -> None:
        """Wake waiters on the channel that received a new message.

        Each ``asyncio.Event.set()`` is wrapped in a try/except so one
        broken waiter never prevents others from being woken.

        Args:
            event_name: The event name (unused).
            **data: Event payload containing ``channel``.
        """
        _ = event_name
        channel = str(data.get("channel", ""))
        for event in self._waiters.get(channel, []):
            try:
                event.set()
            except Exception:
                logger.exception(
                    "Failed to wake waiter on channel %s", channel,
                )

    # ── Await-update helpers ───────────────────────────────────

    async def _count_messages_since(
        self, channel: str, since: str | None = None,
    ) -> int:
        """Count messages in a channel, optionally filtered by timestamp.

        Args:
            channel: Channel name.
            since: Optional ISO 8601 timestamp lower bound (inclusive).

        Returns:
            The number of matching messages.
        """
        assert self.database is not None
        chan_rows = await self.database.execute(
            "SELECT id FROM chat_channels WHERE name = ?",
            (channel,),
        )
        if not chan_rows:
            return 0
        channel_id = str(chan_rows[0]["id"])
        if since:
            rows = await self.database.execute(
                "SELECT COUNT(*) as cnt FROM chat_messages "
                "WHERE channel_id = ? AND created_at >= ?",
                (channel_id, since),
            )
        else:
            rows = await self.database.execute(
                "SELECT COUNT(*) as cnt FROM chat_messages"
                " WHERE channel_id = ?",
                (channel_id,),
            )
        return int(rows[0]["cnt"]) if rows else 0

    async def _get_newest_messages(
        self,
        channel: str,
        limit: int,
        since: str | None = None,
    ) -> list[dict[str, object]]:
        """Return the *limit* most-recent messages, newest first.

        When ``since`` is given only messages at or after that timestamp
        are considered.

        Args:
            channel: Channel name.
            limit: Maximum number of messages to return.
            since: Optional ISO 8601 timestamp lower bound (inclusive).

        Returns:
            List of message dicts in chronological order.
        """
        assert self.database is not None
        chan_rows = await self.database.execute(
            "SELECT id FROM chat_channels WHERE name = ?",
            (channel,),
        )
        if not chan_rows:
            return []
        channel_id = str(chan_rows[0]["id"])
        if since:
            rows = await self.database.execute(
                "SELECT id, channel_id, agent_id, parent_id, content_type,"
                " content, created_at FROM chat_messages"
                " WHERE channel_id = ? AND created_at >= ?"
                " ORDER BY created_at DESC LIMIT ?",
                (channel_id, since, limit),
            )
        else:
            rows = await self.database.execute(
                "SELECT id, channel_id, agent_id, parent_id, content_type,"
                " content, created_at FROM chat_messages"
                " WHERE channel_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (channel_id, limit),
            )
        messages = [
            {
                "id": str(r["id"]),
                "channel_id": str(r["channel_id"]),
                "agent_id": str(r["agent_id"]),
                "parent_id": r.get("parent_id"),
                "content_type": str(r.get("content_type", "text")),
                "content": str(r["content"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]
        messages.reverse()  # Return in chronological order
        return messages

    # ── Tool: chat_await_update ────────────────────────────────

    async def _handle_await_update(
        self,
        channel: str,
        nmsg: int = 1,
        timeout: float = 120.0,  # noqa: ASYNC109
        since: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        """Block until *n* new messages appear in a channel or timeout.

        Use this when waiting for responses or activity.  Returns the
        messages that arrived.

        Args:
            channel: Channel name to watch.
            nmsg: Minimum number of new messages to wait for (>= 1).
            timeout: Seconds to wait before giving up (> 0).
            since: Optional ISO 8601 timestamp — only count messages >= this.

        Returns:
            Dict with ``messages`` list, ``waited`` flag, and ``timed_out`` flag.
        """
        _ = kwargs
        channel_name = channel

        if not channel_name:
            return {
                "error": "VALIDATION_ERROR",
                "message": "Channel name must not be empty",
                "details": {},
                "fix": "Provide a non-empty channel name (e.g. '#general').",
            }
        if nmsg < 1:
            return {
                "error": "VALIDATION_ERROR",
                "message": "nmsg must be at least 1",
                "details": {"nmsg": nmsg},
                "fix": "Set nmsg to 1 or higher.",
            }
        if timeout <= 0:
            return {
                "error": "VALIDATION_ERROR",
                "message": "timeout must be positive",
                "details": {"timeout": timeout},
                "fix": "Set timeout to a positive number.",
            }

        assert self.database is not None

        if len(self._waiters[channel_name]) >= _MAX_WAITERS_PER_CHANNEL:
            return {
                "error": "RESOURCE_LIMIT",
                "message": (
                    f"Too many concurrent waiters on '{channel_name}'"
                    f" (max {_MAX_WAITERS_PER_CHANNEL})"
                ),
                "details": {
                    "channel": channel_name,
                    "current_waiters": len(self._waiters[channel_name]),
                },
                "fix": "Retry later or reduce concurrent waiting agents.",
            }

        event = asyncio.Event()
        self._waiters[channel_name].append(event)
        try:
            return await self._await_loop(channel_name, nmsg, timeout, since, event)
        finally:
            if event in self._waiters[channel_name]:
                self._waiters[channel_name].remove(event)
            if not self._waiters[channel_name]:
                del self._waiters[channel_name]

    async def _await_loop(
        self,
        channel_name: str,
        nmsg: int,
        timeout: float,  # noqa: ASYNC109
        since: str | None,
        event: asyncio.Event,
    ) -> dict[str, object]:
        """Core wait loop — polls until *nmsg* messages exist or timeout.

        Args:
            channel_name: Channel name to query.
            nmsg: Minimum messages required.
            timeout: Maximum seconds to wait.
            since: Optional ISO 8601 timestamp lower bound.
            event: The asyncio.Event to wait on.

        Returns:
            Result dict with messages/waited/timed_out.
        """
        # Fast path — enough messages already present
        existing = await self._count_messages_since(channel_name, since)
        if existing >= nmsg:
            rows = await self._get_newest_messages(channel_name, nmsg, since)
            return {"messages": rows, "waited": False, "timed_out": False}

        # Slow path — loop until enough messages or timeout
        start_mono = time.monotonic()
        while True:
            remaining = timeout - (time.monotonic() - start_mono)
            if remaining <= 0:
                break
            try:
                event.clear()
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                break

            try:
                final = await self._count_messages_since(channel_name, since)
            except apsw.CursorClosedError:
                return {"messages": [], "waited": True, "timed_out": False}

            if final >= nmsg:
                rows = await self._get_newest_messages(channel_name, nmsg, since)
                return {"messages": rows, "waited": True, "timed_out": False}

        # Timed out — one final recheck
        try:
            final = await self._count_messages_since(channel_name, since)
        except apsw.CursorClosedError:
            return {"messages": [], "waited": True, "timed_out": True}

        if final >= nmsg:
            rows = await self._get_newest_messages(channel_name, nmsg, since)
            return {"messages": rows, "waited": True, "timed_out": False}
        return {"messages": [], "waited": True, "timed_out": True}

    # ── Tool definitions ─────────────────────────────────────────

    def get_tools(self) -> list[ToolDef]:
        """Return the four chat plugin tools.

        Returns:
            List of ToolDef instances for post_message, read_messages,
            list_channels, and summarize_channel.
        """
        return [
            ToolDef(
                name="post_message",
                handler=self._handle_post_message,
                description=(
                    "Post a message to a channel. Use this when you"
                    " need to share information with other agents."
                    " Auto-creates the channel if needed. Max length:"
                    " 100,000 characters."
                ),
            ),
            ToolDef(
                name="read_messages",
                handler=self._handle_read_messages,
                description=(
                    "Read message history from a channel. Use this"
                    " when catching up on conversations or checking"
                    " for updates."
                ),
            ),
            ToolDef(
                name="list_channels",
                handler=self._handle_list_channels,
                description=(
                    "List all channels with optional prefix filter."
                    " Use this when discovering what teams are"
                    " discussing — try prefix '#team' for team"
                    " channels."
                ),
            ),
            ToolDef(
                name="summarize_channel",
                handler=self._handle_summarize_channel,
                description=(
                    "Summarize recent channel activity. Use this"
                    " when a channel has too many messages to read"
                    " individually."
                ),
            ),
            ToolDef(
                name="await_update",
                handler=self._handle_await_update,
                description=(
                    "Block until n new messages appear in a channel"
                    " or timeout. Use this when waiting for responses"
                    " or activity. Returns the messages that arrived."
                ),
            ),
        ]

    def get_migrations(self) -> list[str]:
        """Return SQL migrations for chat_channels and chat_messages tables.

        Returns:
            Ordered list of SQL migration strings.
        """
        return get_migrations()

    # ── Tool: chat_post_message ──────────────────────────────────

    async def _handle_post_message(
        self, channel: str, content: str,
        parent_id: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        """Post a message to a channel.

        Use this when you need to share information with other agents.
        Auto-creates the channel if needed. Max length: 100,000 characters.

        Args:
            channel: Channel name (e.g. "#general", "#sprint-planning").
            content: Message body text.
            parent_id: Optional UUID of parent message for threading.

        Returns:
            Dict with ``message_id``, ``channel``, and ``created_at``.
        """
        _ = kwargs  # _agent_id extracted by middleware
        channel_name = channel
        agent_id = str(kwargs.get("_agent_id", "unknown"))

        if not channel_name:
            return {
                "error": "VALIDATION_ERROR",
                "message": "Channel name must not be empty",
                "details": {},
                "fix": "Provide a non-empty channel name (e.g. '#general').",
            }

        if not content:
            return {
                "error": "VALIDATION_ERROR",
                "message": "Message content must not be empty",
                "details": {},
                "fix": "Provide non-empty message content.",
            }

        if len(content) > self.max_message_length:
            return {
                "error": "VALIDATION_ERROR",
                "message": f"Message content exceeds {self.max_message_length} characters",
                "details": {"max_length": self.max_message_length},
                "fix": f"Reduce message length to {self.max_message_length} characters or fewer.",
            }

        # Ensure channel exists (auto-vivify)
        channel_id = await self._ensure_channel(channel_name)
        if channel_id is None:
            return {
                "error": "CHANNEL_LIMIT",
                "message": f"Maximum of {self.max_channels} channels reached",
                "details": {"max_channels": self.max_channels},
                "fix": "Reduce the number of channels or increase max_channels config.",
            }

        # Generate message ID and timestamp
        message_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        assert self.database is not None
        await self.database.execute(
            "INSERT INTO chat_messages "
            "(id, channel_id, agent_id, parent_id, content_type, content, created_at) "
            "VALUES (?, ?, ?, ?, 'text', ?, ?)",
            (message_id, channel_id, agent_id, parent_id, content, now),
        )

        # Emit event
        if self.eventbus is not None:
            await self.eventbus.emit(
                "chat.message.posted",
                channel=channel_name,
                agent_id=agent_id,
                message_id=message_id,
            )

        return {
            "message_id": message_id,
            "channel": channel_name,
            "created_at": now,
        }

    # ── Tool: chat_read_messages ─────────────────────────────────

    async def _handle_read_messages(
        self, channel: str, since: str | None = None,
        limit: int = 3, order: str = "desc",
        **kwargs: object,
    ) -> dict[str, object]:
        """Read message history from a channel.

        Use this to catch up on conversations or check for updates.

        Args:
            channel: Channel name (e.g. "#general").
            since: Optional ISO 8601 timestamp — return only messages >= this time.
            limit: Max messages to return (0-1000). 0 returns empty list. Default 3.
            order: Chronological order ("asc" or "desc"). Default "desc" (newest first).

        Returns:
            Dict with a ``messages`` list.
        """
        _ = kwargs
        channel_name = channel
        max_limit = 1000
        if limit < 0 or limit > max_limit:
            return {
                "error": "VALIDATION_ERROR",
                "message": f"Limit must be between 0 and {max_limit}",
                "details": {"limit": limit},
                "fix": "Set limit between 0 and 1000.",
            }
        order_lower = order.lower()

        if order_lower not in ("asc", "desc"):
            return {
                "error": "VALIDATION_ERROR",
                "message": "Order must be 'asc' or 'desc'",
                "details": {"order": order},
                "fix": "Use 'asc' or 'desc'.",
            }

        if not channel_name:
            return {
                "error": "VALIDATION_ERROR",
                "message": "Channel name must not be empty",
                "details": {},
                "fix": "Provide a non-empty channel name (e.g. '#general').",
            }

        assert self.database is not None

        # Look up channel
        chan_rows = await self.database.execute(
            "SELECT id FROM chat_channels WHERE name = ?",
            (channel_name,),
        )
        if not chan_rows:
            return {"messages": []}

        channel_id: str = str(chan_rows[0]["id"])

        # Build query — direction is validated "asc"/"desc" above
        direction = "ASC" if order_lower == "asc" else "DESC"
        base_sql = (
            "SELECT id, channel_id, agent_id, parent_id, content_type,"
            " content, created_at FROM chat_messages"
        )

        if since:
            stmt = (
                f"{base_sql} WHERE channel_id = ? AND created_at >= ?"
                f" ORDER BY created_at {direction} LIMIT ?"
            )
            rows = await self.database.execute(stmt, (channel_id, since, limit))
        else:
            stmt = (
                f"{base_sql} WHERE channel_id = ?"
                f" ORDER BY created_at {direction} LIMIT ?"
            )
            rows = await self.database.execute(stmt, (channel_id, limit))

        messages = [
            {
                "id": str(r["id"]),
                "channel_id": str(r["channel_id"]),
                "agent_id": str(r["agent_id"]),
                "parent_id": r.get("parent_id"),
                "content_type": str(r.get("content_type", "text")),
                "content": str(r["content"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

        return {"messages": messages}

    # ── Tool: chat_list_channels ─────────────────────────────────

    async def _handle_list_channels(
        self, prefix: str | None = None, **kwargs: object,
    ) -> dict[str, object]:
        """List all channels with optional prefix filter.

        Use this to discover what teams are discussing — try prefix '#team' for team channels.

        Args:
            prefix: Optional prefix to filter channel names (e.g. "#dev" returns "#dev-auth").

        Returns:
            Dict with a ``channels`` list.
        """
        _ = kwargs

        assert self.database is not None

        if prefix:
            rows = await self.database.execute(
                "SELECT id, name, topic, metadata_json, created_at"
                " FROM chat_channels WHERE name LIKE ?"
                " ORDER BY name",
                (f"{prefix}%",),
            )
        else:
            rows = await self.database.execute(
                "SELECT id, name, topic, metadata_json, created_at"
                " FROM chat_channels ORDER BY name",
            )

        channels = []
        for row in rows:
            channel_id: str = str(row["id"])
            # Count messages and get last activity
            count_rows = await self.database.execute(
                "SELECT COUNT(*) as cnt, MAX(created_at) as last_at"
                " FROM chat_messages WHERE channel_id = ?",
                (channel_id,),
            )
            msg_count: int = int(count_rows[0]["cnt"]) if count_rows else 0
            raw_last = count_rows[0].get("last_at") if count_rows else None
            last_at: str | None = str(raw_last) if raw_last else None

            channels.append({
                "name": str(row["name"]),
                "topic": row.get("topic"),
                "message_count": msg_count,
                "last_activity_at": last_at,
            })

        return {"channels": channels}

    # ── Tool: chat_summarize_channel ─────────────────────────────

    async def _handle_summarize_channel(
        self, channel: str, since: str | None = None, **kwargs: object,
    ) -> dict[str, object]:
        """Get a summary of recent channel activity.

        Use when a channel has too many messages to read individually.

        Args:
            channel: Channel name.
            since: Optional ISO 8601 timestamp — only consider messages >= this time.

        Returns:
            Dict with summary, message_count, participants, and time_span_hours.
        """
        _ = kwargs
        channel_name = channel

        if not channel_name:
            return {
                "error": "VALIDATION_ERROR",
                "message": "Channel name must not be empty",
                "details": {},
                "fix": "Provide a non-empty channel name (e.g. '#general').",
            }

        assert self.database is not None

        # Look up channel
        chan_rows = await self.database.execute(
            "SELECT id FROM chat_channels WHERE name = ?",
            (channel_name,),
        )
        if not chan_rows:
            return {
                "error": "CHANNEL_NOT_FOUND",
                "message": f"No channel named '{channel_name}'",
                "details": {},
                "fix": "Create the channel first with chat_post_message.",
            }

        channel_id: str = str(chan_rows[0]["id"])

        # Fetch messages
        if since:
            rows = await self.database.execute(
                "SELECT agent_id, content, created_at"
                " FROM chat_messages WHERE channel_id = ? AND created_at >= ?"
                " ORDER BY created_at ASC",
                (channel_id, since),
            )
        else:
            rows = await self.database.execute(
                "SELECT agent_id, content, created_at"
                " FROM chat_messages WHERE channel_id = ?"
                " ORDER BY created_at ASC",
                (channel_id,),
            )

        if not rows:
            return {
                "summary": f"No messages in '{channel_name}'.",
                "message_count": 0,
                "participants": 0,
                "time_span_hours": 0.0,
            }

        # Compute stats
        participants: set[str] = set()
        for r in rows:
            participants.add(str(r["agent_id"]))

        first_at_str: str = str(rows[0]["created_at"])
        last_at_str: str = str(rows[-1]["created_at"])

        try:
            first_dt = datetime.fromisoformat(first_at_str)
            last_dt = datetime.fromisoformat(last_at_str)
            span_hours = (last_dt - first_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            span_hours = 0.0

        message_count = len(rows)
        participant_count = len(participants)

        # Check for built-in or configured LLM
        if self.use_built_in_llm:
            summary = (
                f"Built-in LLM summarization not yet implemented."
                f" {message_count} message(s) from {participant_count} agent(s)"
                f" over {span_hours:.1f} hour(s)."
            )
        elif self.llm_api_url:
            summary = await self._call_llm(rows)
        else:
            summary = (
                f"{message_count} message(s) from {participant_count} agent(s)"
                f" over {span_hours:.1f} hour(s)"
                f" in '{channel_name}'."
            )

        return {
            "summary": summary,
            "message_count": message_count,
            "participants": participant_count,
            "time_span_hours": round(span_hours, 1),
        }

    # ── Internal helpers ─────────────────────────────────────────

    async def _ensure_channel(self, name: str) -> str | None:
        """Ensure a channel exists by name, creating it if necessary.

        If the channel already exists in the cache, returns its ID.
        Otherwise, checks the database, and if still not found, creates
        a new channel (subject to the channel limit).

        Args:
            name: The channel name to look up or create.

        Returns:
            The channel ID string, or ``None`` if the channel limit
            would be exceeded.
        """
        assert self.database is not None

        # Critical section: check-then-create under a lock to prevent
        # concurrent SELECT-then-INSERT races for the same channel name.
        async with self._channel_lock:
            # Check cache first (doubles as fast path)
            if name in self._channel_names:
                cached = await self.database.execute(
                    "SELECT id FROM chat_channels WHERE name = ?",
                    (name,),
                )
                if cached:
                    return str(cached[0]["id"])

            # Check database
            existing = await self.database.execute(
                "SELECT id FROM chat_channels WHERE name = ?",
                (name,),
            )
            if existing:
                self._channel_names.add(name)
                return str(existing[0]["id"])

            # Create new channel (check limit first)
            cnt_rows = await self.database.execute(
                "SELECT COUNT(*) as cnt FROM chat_channels",
            )
            current_count: int = int(cnt_rows[0]["cnt"]) if cnt_rows else 0
            if current_count >= self.max_channels:
                return None

            channel_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            topic: str | None = None
            if name == _CHANNEL_GENERAL:
                topic = "General discussion for all agents"

            await self.database.execute(
                "INSERT INTO chat_channels (id, name, topic, metadata_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (channel_id, name, topic, None, now),
            )
            self._channel_names.add(name)
            return channel_id

    async def _post_system_message(
        self, channel: str, content: str,
    ) -> dict[str, object] | None:
        """Post a message on behalf of the system agent.

        Args:
            channel: The channel to post to.
            content: The message content.

        Returns:
            The result from post_message, or ``None`` if it fails.
        """
        try:
            return await self._handle_post_message(
                channel=channel, content=content, _agent_id=_SYSTEM_AGENT_ID,
            )
        except Exception:
            logger.exception("Failed to post system message to %s", channel)
            return None

    async def _call_llm(self, messages: Sequence[dict[str, object]]) -> str:
        """Call a configurable LLM endpoint for summarization.

        POSTs the formatted messages to the configured LLM API URL using
        the OpenAI-compatible chat completions format.

        Args:
            messages: List of message dicts with ``agent_id``, ``content``,
                and ``created_at`` keys.

        Returns:
            The LLM-generated summary string.
        """
        formatted = "\n".join(
            f"[{m.get('created_at', '')}] {m.get('agent_id', '?')}: {m.get('content', '')}"
            for m in messages[-50:]  # Limit context to last 50 messages
        )

        payload_dict: dict[str, object] = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": self.llm_system_prompt},
                {"role": "user", "content": f"Messages:\n{formatted}"},
            ],
            "max_tokens": self.llm_max_tokens,
        }
        payload = json.dumps(payload_dict).encode()

        # Validate URL scheme (reject file:// and other unexpected schemes)
        parsed_url = urllib.parse.urlparse(self.llm_api_url)
        if parsed_url.scheme not in ("http", "https"):
            return f"Invalid LLM API URL scheme: '{parsed_url.scheme}'"

        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self.llm_api_key:
            headers["Authorization"] = f"Bearer {self.llm_api_key}"

        req = urllib.request.Request(  # noqa: S310
            self.llm_api_url,
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            response = await asyncio.to_thread(urllib.request.urlopen, req)
            body = response.read().decode()
            result = json.loads(body)
            choices = result.get("choices", [])
            if choices:
                return str(choices[0]["message"]["content"])
            return f"LLM returned no choices: {body[:200]}"
        except urllib.error.URLError as exc:
            logger.exception("LLM call failed")
            return f"LLM call failed: {exc.reason}"
        except (json.JSONDecodeError, KeyError) as exc:
            logger.exception("LLM response parse failed")
            return f"LLM response parse failed: {exc}"

    async def _refresh_channel_cache(self) -> None:
        """Reload the channel name cache from the database."""
        assert self.database is not None
        rows = await self.database.execute(
            "SELECT name FROM chat_channels ORDER BY name",
        )
        self._channel_names = {str(r["name"]) for r in rows}
