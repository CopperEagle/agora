"""Tests for PluginLoader — discover, import, instantiate plugins from config."""

from __future__ import annotations

import importlib
import types
from collections.abc import AsyncGenerator

import pytest

from agora.backbone import AgoraPlugin, ToolDef
from agora.backbone.database import Database
from agora.backbone.eventbus import EventBus
from agora.backbone.loader import PluginLoader

# ── Mock plugin for testing ─────────────────────────────────────


class MockPlugin(AgoraPlugin):
    """Mock plugin used across loader tests."""

    name = "mock"
    version = "1.0.0"
    description = "Mock plugin for testing"

    def __init__(self) -> None:
        self.load_called = False
        self.startup_called = False
        self.load_config: dict[str, object] | None = None

    async def on_load(self, _config: dict[str, object]) -> None:
        self.load_called = True
        self.load_config = _config

    async def on_startup(self) -> None:
        self.startup_called = True

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="mock_tool",
                handler=self._handler,
                description="Mock tool",
            ),
        ]

    async def _handler(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"result": "ok"}

    def get_migrations(self) -> list[str]:
        return ["CREATE TABLE IF NOT EXISTS mock_data (id TEXT PRIMARY KEY)"]


class SecondMockPlugin(AgoraPlugin):
    """Second mock plugin for duplicate/merge tests."""

    name = "second"
    version = "1.0.0"
    description = "Second mock plugin"

    def __init__(self) -> None:
        self.load_called = False
        self.startup_called = False

    async def on_load(self, _config: dict[str, object]) -> None:
        self.load_called = True

    async def on_startup(self) -> None:
        self.startup_called = True

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="second_tool",
                handler=self._handler,
                description="Second tool",
            ),
        ]

    async def _handler(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"result": "second"}

    def get_migrations(self) -> list[str]:
        return ["CREATE TABLE IF NOT EXISTS second_data (id TEXT PRIMARY KEY)"]


class DuplicateToolPlugin(AgoraPlugin):
    """Plugin that registers a tool with the same name as MockPlugin."""

    name = "dup"
    version = "1.0.0"
    description = "Plugin with duplicate tool name"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="mock_tool",
                handler=self._handler,
                description="Duplicate tool",
            ),
        ]

    async def _handler(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"result": "dup"}


class NoToolsPlugin(AgoraPlugin):
    """Plugin that provides no tools."""

    name = "notools"
    version = "1.0.0"
    description = "Plugin with no tools"

    def get_migrations(self) -> list[str]:
        return ["CREATE TABLE IF NOT EXISTS notools_data (id TEXT PRIMARY KEY)"]


# ── Helpers ─────────────────────────────────────────────────────


def _make_mock_module(plugin_class: type[AgoraPlugin]) -> types.ModuleType:
    """Create a temporary module containing the given plugin class.

    The class is assigned to ``ChatPlugin`` attribute to match the
    config's ``class_name`` field convention.

    Args:
        plugin_class: The plugin class to embed in the module.

    Returns:
        A ``types.ModuleType`` with a ``ChatPlugin`` attribute.
    """
    module = types.ModuleType("test_plugin_module")
    module.ChatPlugin = plugin_class  # type: ignore[attr-defined]
    return module


def _plugin_config(
    module: str = "test_plugin_module",
    class_name: str = "ChatPlugin",
    name: str = "mock",
    enabled: bool = True,
    config: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a single plugin config entry.

    Args:
        module: Python module path to import.
        class_name: Class name within the module.
        name: Plugin name string.
        enabled: Whether the plugin is enabled.
        config: Optional plugin-specific config dict.

    Returns:
        A dict matching the plugin config format expected by
        ``PluginLoader.load_plugins()``.
    """
    return {
        "name": name,
        "enabled": enabled,
        "module": module,
        "class_name": class_name,
        "config": config or {},
    }


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def database() -> AsyncGenerator[Database, None]:
    """Provide an in-memory Database instance, closed after each test.

    Yields:
        A connected ``Database`` bound to ``:memory:``.
    """
    db = Database(":memory:")
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def eventbus() -> EventBus:
    """Provide a fresh EventBus instance per test."""
    return EventBus()


# ── Tests ───────────────────────────────────────────────────────


async def test_load_plugin_calls_on_load(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """on_load is called with the config dict from the config entry."""
    mock_module = _make_mock_module(MockPlugin)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: mock_module,
    )

    loader = PluginLoader(database=database)
    plugin_config = _plugin_config(config={"channel": "general"})

    plugins, _tools = await loader.load_plugins([plugin_config])

    assert len(plugins) == 1
    plugin = plugins[0]
    assert isinstance(plugin, MockPlugin)
    assert plugin.load_called is True
    assert plugin.load_config == {"channel": "general"}


async def test_load_plugin_executes_migrations(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Plugin migrations are run via database.run_migrations."""
    mock_module = _make_mock_module(MockPlugin)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: mock_module,
    )

    loader = PluginLoader(database=database)
    plugins, _tools = await loader.load_plugins([_plugin_config()])

    assert len(plugins) == 1
    # Verify the migration created the table
    rows = await database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mock_data'",
    )
    assert len(rows) == 1


async def test_load_plugin_calls_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """on_startup is called after migrations complete."""
    mock_module = _make_mock_module(MockPlugin)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: mock_module,
    )

    loader = PluginLoader(database=database)
    plugins, _tools = await loader.load_plugins([_plugin_config()])

    plugin = plugins[0]
    assert isinstance(plugin, MockPlugin)
    assert plugin.startup_called is True


async def test_load_plugin_collects_tools(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Tools returned by get_tools are collected into the tools dict."""
    mock_module = _make_mock_module(MockPlugin)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: mock_module,
    )

    loader = PluginLoader(database=database)
    _plugins, tools = await loader.load_plugins([_plugin_config()])

    assert "mock_tool" in tools
    assert tools["mock_tool"].description == "Mock tool"
    assert callable(tools["mock_tool"].handler)


async def test_duplicate_tool_names_raise_error(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Two plugins registering the same tool name raises ValueError."""
    call_count = 0

    def _import_side_effect(name: str) -> types.ModuleType:
        nonlocal call_count
        call_count += 1
        module = types.ModuleType(name)
        if call_count == 1:
            module.ChatPlugin = MockPlugin  # type: ignore[attr-defined]
        else:
            module.ChatPlugin = DuplicateToolPlugin  # type: ignore[attr-defined]
        return module

    monkeypatch.setattr(importlib, "import_module", _import_side_effect)

    loader = PluginLoader(database=database)
    config = [
        _plugin_config(name="plugin_a"),
        _plugin_config(name="plugin_b"),
    ]

    with pytest.raises(ValueError, match="Duplicate tool name: mock_tool"):
        await loader.load_plugins(config)


async def test_disabled_plugin_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Plugin with enabled=False is skipped entirely."""
    import_called = False

    def _import_side_effect(_name: str) -> types.ModuleType:
        nonlocal import_called
        import_called = True
        return _make_mock_module(MockPlugin)

    monkeypatch.setattr(importlib, "import_module", _import_side_effect)

    loader = PluginLoader(database=database)
    config = [_plugin_config(enabled=False)]

    plugins, tools = await loader.load_plugins(config)

    assert plugins == []
    assert tools == {}
    assert import_called is False


async def test_empty_config_returns_empty(
    database: Database,
) -> None:
    """Empty config list returns empty plugins and tools."""
    loader = PluginLoader(database=database)

    plugins, tools = await loader.load_plugins([])

    assert plugins == []
    assert tools == {}


async def test_module_not_found_raises_importerror(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Bad module path raises ImportError with clear message."""
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(
            ModuleNotFoundError("No module named 'nonexistent.module'"),
        ),
    )

    loader = PluginLoader(database=database)
    config = [_plugin_config(module="nonexistent.module")]

    with pytest.raises(ImportError, match=r"nonexistent\.module"):
        await loader.load_plugins(config)


async def test_class_not_found_raises_attributeerror(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Module missing the class_name attribute raises AttributeError."""
    module = types.ModuleType("test_module")
    # Intentionally do not set ChatPlugin attribute

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: module,
    )

    loader = PluginLoader(database=database)
    config = [_plugin_config(class_name="ChatPlugin")]

    with pytest.raises(AttributeError, match="ChatPlugin"):
        await loader.load_plugins(config)


async def test_plugin_load_order(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Plugins are loaded in config order."""
    load_order: list[str] = []

    class AlphaPlugin(AgoraPlugin):
        name = "alpha"
        version = "1.0.0"

        async def on_load(self, _config: dict[str, object]) -> None:
            load_order.append("alpha")

    class BetaPlugin(AgoraPlugin):
        name = "beta"
        version = "1.0.0"

        async def on_load(self, _config: dict[str, object]) -> None:
            load_order.append("beta")

    class GammaPlugin(AgoraPlugin):
        name = "gamma"
        version = "1.0.0"

        async def on_load(self, _config: dict[str, object]) -> None:
            load_order.append("gamma")

    def _import_side_effect(name: str) -> types.ModuleType:
        mapping: dict[str, type[AgoraPlugin]] = {
            "alpha_mod": AlphaPlugin,
            "beta_mod": BetaPlugin,
            "gamma_mod": GammaPlugin,
        }
        cls = mapping[name]
        module = types.ModuleType(name)
        module.ChatPlugin = cls  # type: ignore[attr-defined]
        return module

    monkeypatch.setattr(importlib, "import_module", _import_side_effect)

    loader = PluginLoader(database=database)
    config = [
        _plugin_config(module="gamma_mod", name="gamma"),
        _plugin_config(module="alpha_mod", name="alpha"),
        _plugin_config(module="beta_mod", name="beta"),
    ]

    plugins, _tools = await loader.load_plugins(config)

    assert len(plugins) == 3
    assert load_order == ["gamma", "alpha", "beta"]


async def test_multiple_plugins_tools_merged(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Tools from multiple plugins are merged into one dict."""
    call_count = 0

    def _import_side_effect(name: str) -> types.ModuleType:
        nonlocal call_count
        call_count += 1
        module = types.ModuleType(name)
        if call_count == 1:
            module.ChatPlugin = MockPlugin  # type: ignore[attr-defined]
        else:
            module.ChatPlugin = SecondMockPlugin  # type: ignore[attr-defined]
        return module

    monkeypatch.setattr(importlib, "import_module", _import_side_effect)

    loader = PluginLoader(database=database)
    config = [
        _plugin_config(name="plugin_a"),
        _plugin_config(name="plugin_b"),
    ]

    plugins, tools = await loader.load_plugins(config)

    assert len(plugins) == 2
    assert "mock_tool" in tools
    assert "second_tool" in tools
    assert tools["mock_tool"].description == "Mock tool"
    assert tools["second_tool"].description == "Second tool"


async def test_no_tools_plugin(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Plugin with no tools produces empty tools dict entry."""
    module = types.ModuleType("notools_mod")
    module.ChatPlugin = NoToolsPlugin  # type: ignore[attr-defined]

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: module,
    )

    loader = PluginLoader(database=database)
    plugins, tools = await loader.load_plugins([_plugin_config()])

    assert len(plugins) == 1
    assert tools == {}


async def test_plugin_name_not_in_config(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """Plugin name comes from the class attribute, not the config entry."""
    mock_module = _make_mock_module(MockPlugin)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: mock_module,
    )

    loader = PluginLoader(database=database)
    plugins, _tools = await loader.load_plugins([_plugin_config(name="different_name")])

    assert len(plugins) == 1
    assert plugins[0].name == "mock"  # class attribute wins


async def test_plugin_receives_database_and_eventbus(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    eventbus: EventBus,
) -> None:
    """Plugin instance gets database and eventbus injected by PluginLoader."""
    mock_module = _make_mock_module(MockPlugin)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: mock_module,
    )

    loader = PluginLoader(database=database, eventbus=eventbus)
    plugins, _tools = await loader.load_plugins([_plugin_config()])

    assert len(plugins) == 1
    plugin = plugins[0]
    assert plugin.database is database
    assert plugin.eventbus is eventbus
