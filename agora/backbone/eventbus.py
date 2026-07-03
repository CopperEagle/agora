"""In-process async event bus for cross-plugin communication."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[None]]  # type: ignore[explicit-any]


class EventBus:
    """In-process async event bus for cross-plugin communication.

    Subscribers are async callables. emit() runs all subscribers concurrently
    with crash isolation — one failing subscriber never affects others.

    Example::

        bus = EventBus()

        async def on_message(event_name: str, **data: object) -> None:
            print(f"Got {event_name}: {data}")

        bus.subscribe("chat.message", on_message)
        await bus.emit("chat.message", content="hello")
    """

    def __init__(self) -> None:
        """Initialize an empty EventBus."""
        self._subscribers: defaultdict[str, list[Handler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(self, event_name: str, handler: Handler) -> None:
        """Register a handler for an event.

        The handler receives keyword arguments: event_name as a keyword arg
        plus any data passed to emit().

        Args:
            event_name: The event channel to subscribe to (e.g. 'agent.registered').

            handler: An async callable that will be invoked on each emit.

        """
        self._subscribers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: Handler) -> None:
        """Remove a previously registered handler.

        Args:
            event_name: The event channel to unsubscribe from.

            handler: The handler to remove. If not found, this is a no-op.

        """
        handlers = self._subscribers.get(event_name)
        if handlers is not None:
            with contextlib.suppress(ValueError):
                handlers.remove(handler)

    async def emit(self, event_name: str, /, **data: object) -> None:
        """Emit event to all subscribers.

        Runs handlers concurrently with crash isolation — uses
        asyncio.gather with return_exceptions=True so one handler
        crashing never prevents others from executing.

        Args:
            event_name: The event channel name (positional-only).

            **data: Arbitrary keyword data forwarded to each handler.

        """
        async with self._lock:
            # Snapshot the list to avoid mutation during iteration.
            handlers = list(self._subscribers.get(event_name, ()))

        if not handlers:
            return

        tasks = [self._safe_dispatch(event_name, handler, data) for handler in handlers]
        await asyncio.gather(*tasks)

    async def _safe_dispatch(
        self,
        event_name: str,
        handler: Handler,
        data: dict[str, object],
    ) -> None:
        """Run a single handler, catching and logging any exception.

        Args:
            event_name: The event being dispatched.

            handler: The handler to invoke.

            data: Keyword data to pass to the handler.

        """
        try:
            await handler(event_name=event_name, **data)
        except Exception:
            logger.exception(
                "Handler %s crashed on event '%s'",
                handler.__qualname__,
                event_name,
            )
