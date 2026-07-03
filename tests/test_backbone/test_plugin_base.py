"""Tests for AgoraPlugin base class and ToolDef dataclass."""

from collections.abc import Awaitable

import pytest

from agora.backbone import AgoraPlugin, ToolDef


def test_tool_def_dataclass() -> None:
    """ToolDef can be created with name, handler, description."""
    async def dummy_handler(*_: object, **__: object) -> dict[str, object]:
        return {"ok": "true"}

    tool = ToolDef(
        name="test_tool",
        handler=dummy_handler,
        description="A test tool",
    )

    assert tool.name == "test_tool"
    assert tool.handler is dummy_handler
    assert tool.description == "A test tool"


def test_tool_def_default_description() -> None:
    """ToolDef description defaults to empty string."""
    async def dummy_handler(*_: object, **__: object) -> dict[str, object]:
        return {"ok": "true"}

    tool = ToolDef(name="test_tool", handler=dummy_handler)

    assert tool.description == ""


def test_tool_def_is_frozen() -> None:
    """ToolDef dataclass is frozen (immutable)."""
    async def dummy_handler(*_: object, **__: object) -> dict[str, object]:
        return {"ok": "true"}

    tool = ToolDef(name="test_tool", handler=dummy_handler)

    with pytest.raises(AttributeError):
        tool.name = "changed"  # type: ignore[misc]


def test_tool_def_type_signature() -> None:
    """ToolDef handler type is Callable[..., Awaitable[dict]]."""
    async def handler(*_: object, **__: object) -> dict[str, object]:
        return {"count": 42}

    tool = ToolDef(name="typed_tool", handler=handler)

    result = tool.handler()
    assert isinstance(result, Awaitable)


class TestAgoraPlugin:
    """Tests for AgoraPlugin base class."""

    def test_instantiate_minimal_plugin(self) -> None:
        """Can instantiate a minimal plugin subclass with just name and get_tools."""
        class MinimalPlugin(AgoraPlugin):
            name = "minimal"

            def get_tools(self) -> list[ToolDef]:
                return []

        plugin = MinimalPlugin()
        assert plugin.name == "minimal"
        assert plugin.version == "0.1.0"
        assert plugin.description == ""

    def test_instantiate_custom_plugin(self) -> None:
        """Can instantiate a plugin with custom name, version, description."""
        class CustomPlugin(AgoraPlugin):
            name = "custom"
            version = "1.2.3"
            description = "A custom plugin"

        plugin = CustomPlugin()
        assert plugin.name == "custom"
        assert plugin.version == "1.2.3"
        assert plugin.description == "A custom plugin"

    def test_get_tools_defaults_empty(self) -> None:
        """Plugin without override returns empty list from get_tools."""
        class DefaultPlugin(AgoraPlugin):
            name = "default"

        plugin = DefaultPlugin()
        assert plugin.get_tools() == []

    def test_get_migrations_defaults_empty(self) -> None:
        """Plugin without override returns empty list from get_migrations."""
        class DefaultPlugin(AgoraPlugin):
            name = "default"

        plugin = DefaultPlugin()
        assert plugin.get_migrations() == []

    @pytest.mark.asyncio
    async def test_optional_hooks_are_noops(self) -> None:
        """Calling lifecycle hooks on a default plugin does not raise."""
        class NoopPlugin(AgoraPlugin):
            name = "noop"

        plugin = NoopPlugin()

        # None of these should raise
        await plugin.on_load({})
        await plugin.on_startup()
        await plugin.on_shutdown()
        await plugin.on_agent_register("agent-123")
        await plugin.on_agent_disconnect("agent-456")

    @pytest.mark.asyncio
    async def test_custom_plugin_with_tools_and_migrations(self) -> None:
        """A custom plugin with tools and migrations returns them correctly."""
        async def my_handler(*_: object, **__: object) -> dict[str, object]:
            return {"result": "done"}

        class MyPlugin(AgoraPlugin):
            name = "myplugin"
            version = "2.0.0"
            description = "My test plugin"

            def get_tools(self) -> list[ToolDef]:
                return [
                    ToolDef(name="do_stuff", handler=my_handler, description="Does stuff"),
                ]

            def get_migrations(self) -> list[str]:
                return [
                    "CREATE TABLE IF NOT EXISTS myplugin_data (id TEXT PRIMARY KEY, val TEXT);",
                ]

        plugin = MyPlugin()

        tools = plugin.get_tools()
        assert len(tools) == 1
        assert tools[0].name == "do_stuff"
        assert tools[0].handler is my_handler
        assert tools[0].description == "Does stuff"

        migrations = plugin.get_migrations()
        assert len(migrations) == 1
        assert "CREATE TABLE" in migrations[0]
        assert "myplugin_data" in migrations[0]

    @pytest.mark.asyncio
    async def test_on_load_receives_config(self) -> None:
        """on_load receives the config dict and a plugin can store it."""
        class ConfigPlugin(AgoraPlugin):
            name = "config_plugin"
            loaded_config: dict[str, object] | None = None

            async def on_load(self, config: dict[str, object]) -> None:
                self.loaded_config = config

        plugin = ConfigPlugin()
        await plugin.on_load({"key": "value", "number": 42})
        assert plugin.loaded_config == {"key": "value", "number": 42}

    @pytest.mark.asyncio
    async def test_can_chain_multiple_lifecycle_hooks(self) -> None:
        """Multiple lifecycle hooks can be called in sequence without error."""
        class ChainPlugin(AgoraPlugin):
            name = "chain"
            startup_count = 0
            shutdown_count = 0

            async def on_startup(self) -> None:
                self.startup_count += 1

            async def on_shutdown(self) -> None:
                self.shutdown_count += 1

        plugin = ChainPlugin()
        await plugin.on_load({})
        await plugin.on_startup()
        assert plugin.startup_count == 1
        await plugin.on_startup()
        assert plugin.startup_count == 2
        await plugin.on_shutdown()
        assert plugin.shutdown_count == 1
