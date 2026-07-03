"""Plugin loader — discover, import, and instantiate plugins from config.

The PluginLoader is a backbone component (NOT a plugin itself) responsible
for the plugin lifecycle: import → instantiate → on_load → migrations →
on_startup → collect tools.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from agora.backbone import AgoraPlugin, ToolDef
from agora.backbone.database import Database
from agora.backbone.eventbus import EventBus

logger = logging.getLogger(__name__)


class PluginLoader:
    """Backbone component that loads plugins from configuration.

    Responsible for dynamic module import, instantiation, and executing
    the full plugin lifecycle (on_load → migrations → on_startup → tools).

    Attributes:
        database: The shared database instance for migration execution.

        eventbus: Optional event bus for cross-plugin communication.

    """

    def __init__(
        self,
        database: Database,
        eventbus: EventBus | None = None,
    ) -> None:
        """Initialize the PluginLoader.

        Args:
            database: Shared database for running plugin migrations.

            eventbus: Optional event bus for cross-plugin communication.

        """
        self._database = database
        self._eventbus = eventbus

    async def load_plugins(  # type: ignore[explicit-any]
        self,
        config: list[dict[str, Any]],
    ) -> tuple[list[AgoraPlugin], dict[str, ToolDef]]:
        """Load and start all enabled plugins from config.

        For each enabled plugin in config order:

        1. Import the module and resolve the class
        2. Instantiate the plugin (no-arg constructor)
        3. Call on_load with the plugin-specific config
        4. Run database migrations
        5. Call on_startup
        6. Collect tools (detecting duplicates)

        Disabled plugins (enabled=False) are skipped silently.

        Args:
            config: List of plugin config dicts. Each must contain
                ``name``, ``enabled``, ``module``, ``class_name``,
                and optionally ``config``.

        Returns:
            Tuple of (plugins_list, tools_dict) where plugins_list
            is ordered by config entry order, and tools_dict maps
            tool names to ToolDef instances.

        Raises:
            ImportError: If a plugin module cannot be imported.

            AttributeError: If the class name is not found in the module.

            ValueError: If two plugins register tools with the same name.

        """
        plugins: list[AgoraPlugin] = []
        tools: dict[str, ToolDef] = {}

        for entry in config:
            if not entry.get("enabled", True):
                continue

            plugin = await self._load_single(entry)
            plugins.append(plugin)

            for tool_def in plugin.get_tools():
                if tool_def.name in tools:
                    msg = f"Duplicate tool name: {tool_def.name}"
                    raise ValueError(msg)
                tools[tool_def.name] = tool_def

        return plugins, tools

    async def _load_single(  # type: ignore[explicit-any]
        self,
        entry: dict[str, Any],
    ) -> AgoraPlugin:
        """Import, instantiate, and run the full lifecycle for one plugin.

        Args:
            entry: A single plugin config dict with ``module``,
                ``class_name``, ``name``, and ``config`` keys.

        Returns:
            The fully initialized plugin instance.

        Raises:
            ImportError: If the module cannot be imported.

            AttributeError: If the class is not found in the module.

        """
        module_path: str = entry["module"]
        class_name: str = entry["class_name"]
        plugin_name: str = entry["name"]
        plugin_config: dict[str, object] = entry.get("config", {})

        logger.info("Loading plugin '%s' from %s.%s", plugin_name, module_path, class_name)

        # 1. Import the module
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            msg = f"Cannot import module '{module_path}': {exc}"
            raise ImportError(msg) from exc

        # 2. Resolve the class
        try:
            plugin_cls: type[AgoraPlugin] = getattr(module, class_name)
        except AttributeError as exc:
            msg = f"Class '{class_name}' not found in module '{module_path}'"
            raise AttributeError(msg) from exc

        # 3. Instantiate (no-arg constructor)
        plugin: AgoraPlugin = plugin_cls()

        # 4. on_load — parse and validate config
        await plugin.on_load(plugin_config)

        # 5. Run migrations
        migrations = plugin.get_migrations()
        if migrations:
            await self._database.run_migrations(plugin.name, migrations)

        # 6. on_startup — initialize connections, caches
        await plugin.on_startup()

        return plugin
