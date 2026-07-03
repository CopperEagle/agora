"""Shared test fixtures for The Agora.

Provides:
- **db**: In-memory APSW SQLite connection with WAL mode enabled.
- **eventbus**: Placeholder — will create an ``EventBus`` instance once
  ``agora/backbone/eventbus.py`` is implemented.
- **server**: Placeholder — will create an ``AgoraServer`` with
  ``skip_transport=True`` once ``agora/backbone/server.py`` is implemented.
"""

from collections.abc import Iterator

import apsw
import pytest

# Ignore test files that require not-yet-implemented backbone modules.
# Remove entries from this list as the corresponding modules are built.
collect_ignore: list[str] = []

# ── Database fixture ──────────────────────────────────────────────


@pytest.fixture
def db() -> Iterator[apsw.Connection]:
    """Create an in-memory SQLite database via APSW with WAL mode.

    Each test gets a fresh, isolated in-memory database.  WAL journal
    mode is enabled to match production settings.

    Yields:
        An open ``apsw.Connection`` bound to ``:memory:``.
    """
    conn = apsw.Connection(":memory:")
    conn.execute("PRAGMA journal_mode=wal")
    try:
        yield conn
    finally:
        conn.close()


# ── EventBus fixture (placeholder) ────────────────────────────────


@pytest.fixture
def eventbus() -> None:
    """Provide an ``EventBus`` instance for pub/sub coordination.

    .. todo::
        Replace the body with::

            from agora.backbone.eventbus import EventBus

            bus = EventBus()
            bus.start()
            yield bus
            bus.shutdown()

        once ``agora/backbone/eventbus.py`` is implemented.

    Raises:
        NotImplementedError: Always — this is a placeholder.
    """
    msg = (
        "eventbus fixture is a placeholder. "
        "Implement agora/backbone/eventbus.py first, then replace"
        " the body with:\n"
        "    from agora.backbone.eventbus import EventBus\n\n"
        "    bus = EventBus()\n"
        "    bus.start()\n"
        "    yield bus\n"
        "    bus.shutdown()"
    )
    raise NotImplementedError(msg)


# ── Server fixture (placeholder) ──────────────────────────────────


@pytest.fixture
def server() -> None:
    """Provide an ``AgoraServer`` that skips real transport binding.

    .. todo::
        Replace the body with::

            from agora.backbone.server import AgoraServer

            srv = AgoraServer(config={"plugins": []}, skip_transport=True)
            await srv.start()
            yield srv
            await srv.stop()

        once ``agora/backbone/server.py`` is implemented.

    Raises:
        NotImplementedError: Always — this is a placeholder.
    """
    msg = (
        "server fixture is a placeholder. "
        "Implement agora/backbone/server.py first, then replace"
        " the body with:\n"
        "    from agora.backbone.server import AgoraServer\n\n"
        "    srv = AgoraServer(config={'plugins': []}, skip_transport=True)\n"
        "    await srv.start()\n"
        "    yield srv\n"
        "    await srv.stop()"
    )
    raise NotImplementedError(msg)


# ── Pytest configuration hook ────────────────────────────────────


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest defaults.

    Sets ``asyncio_mode = "auto"`` as a safety net in case the
    ``pyproject.toml`` setting is not picked up by all runners.
    """
    config.option.asyncio_mode = "auto"
