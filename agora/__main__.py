"""Agora MCP coordination server — CLI entry point.

Start with::

    python -m agora
    # or
    uv run python -m agora

Configuration via ``agora.config.json`` or ``AGORA_CONFIG`` env var.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from agora.backbone.server import AgoraServer

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG: dict[str, object] = {
    "db_path": "agora.db",
    "plugins": [
        {
            "name": "chat",
            "enabled": True,
            "module": "agora.plugins.chat",
            "class_name": "ChatPlugin",
            "config": {"max_message_length": 100000, "max_channels": 1000},
        },
    ],
}

_CONFIG_CANDIDATES: tuple[Path, ...] = (
    Path.cwd() / "agora.config.json",
    Path.home() / ".config" / "agora" / "config.json",
)


def _find_config() -> dict[str, object]:
    """Locate and load the Agora config file.

    Priority:
    1. ``AGORA_CONFIG`` env var pointing to a JSON file.
    2. ``./agora.config.json`` in the current working directory.
    3. ``~/.config/agora/config.json``.

    Returns:
        Parsed config dict, or the default config if none found.

    """
    env_path = os.environ.get("AGORA_CONFIG")
    if env_path:
        path = Path(env_path)
        if path.exists():
            with path.open() as f:
                return json.load(f)  # type: ignore[no-any-return]
        logger.warning("AGORA_CONFIG=%s not found, falling back", env_path)

    for candidate in _CONFIG_CANDIDATES:
        if candidate.exists():
            with candidate.open() as f:
                return json.load(f)  # type: ignore[no-any-return]

    logger.info("No config file found, using defaults")
    return dict(_DEFAULT_CONFIG)


def _setup_logging() -> None:
    """Configure root logging from the ``AGORA_LOG_LEVEL`` env var."""
    log_level = os.environ.get("AGORA_LOG_LEVEL", "INFO").upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _run() -> None:
    """Load config, create server, and run until shutdown."""
    _setup_logging()
    config = _find_config()

    server = AgoraServer(config)
    db_path = config.get("db_path", "agora.db")
    plugins = config.get("plugins", [])
    plugin_count = len(plugins) if isinstance(plugins, list) else 0
    logger.info("Starting Agora server (db=%s, plugins=%d)", db_path, plugin_count)

    try:
        await server.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Server shutting down...")
    finally:
        await server.stop()


def main() -> None:
    """Entry point for ``python -m agora``."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
