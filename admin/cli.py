"""Agora database admin TUI — inspect agents, channels, and messages."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import ClassVar

import apsw
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Label, Static, Tree

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Expected schema
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = ["agents", "chat_channels", "chat_messages"]

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _row_to_dict(cursor: apsw.Cursor, row: tuple[object, ...]) -> dict[str, object]:
    """Convert a single result row into a dict keyed by column names.

    Args:
        cursor: The apsw cursor whose ``getdescription`` reflects the last query.
        row: A single row tuple from the result set.

    Returns:
        A dict mapping column names to their values.

    """
    desc = cursor.getdescription()
    return {col[0]: row[i] for i, col in enumerate(desc)}


# ---------------------------------------------------------------------------
# Schema verification
# ---------------------------------------------------------------------------


def verify_schema(conn: apsw.Connection) -> list[str]:
    """Check that the database contains all expected tables.

    Args:
        conn: An apsw connection to the Agora database.

    Returns:
        A list of missing table names.  An empty list means the schema is
        complete.

    """
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {row[0] for row in cur}
    return [t for t in _EXPECTED_TABLES if t not in existing]


# ---------------------------------------------------------------------------
# Agent queries
# ---------------------------------------------------------------------------


def query_agents(
    conn: apsw.Connection,
    role_filter: str | None = None,
) -> list[dict[str, object]]:
    """Return all registered agents, optionally filtered by role.

    JSON columns ``capabilities`` and ``manifest`` are deserialized into
    Python objects.  ``None`` values are rendered as ``"-"`` for
    display-friendly output.

    Args:
        conn: An apsw connection to the Agora database.
        role_filter: If provided, only return agents whose ``role`` column
            matches this value exactly.

    Returns:
        A list of dicts, one per agent.

    """
    cur = conn.cursor()
    if role_filter is not None:
        cur.execute("SELECT * FROM agents WHERE role = ?", (role_filter,))
    else:
        cur.execute("SELECT * FROM agents")

    results: list[dict[str, object]] = []
    for row in cur:
        d = _row_to_dict(cur, row)

        # Deserialize JSON columns
        for key in ("capabilities", "manifest"):
            val = d.get(key)
            if val is not None:
                d[key] = json.loads(val)

        # Render None as display-friendly dash
        for key, val in d.items():
            if val is None:
                d[key] = "-"

        results.append(d)

    return results


# ---------------------------------------------------------------------------
# Channel queries
# ---------------------------------------------------------------------------


def query_channels(
    conn: apsw.Connection,
    name_prefix: str | None = None,
) -> list[dict[str, object]]:
    """Return channels with message counts and last activity timestamp.

    Args:
        conn: An apsw connection to the Agora database.
        name_prefix: If provided, only return channels whose ``name`` starts
            with this prefix (case-sensitive).

    Returns:
        A list of dicts, one per channel, ordered alphabetically by name.

    """
    cur = conn.cursor()

    base_sql = (
        "SELECT c.*, COUNT(m.id) AS message_count, "
        "MAX(m.created_at) AS last_activity_at "
        "FROM chat_channels c "
        "LEFT JOIN chat_messages m ON c.id = m.channel_id "
    )

    if name_prefix is not None:
        sql = base_sql + "WHERE c.name LIKE ? GROUP BY c.id ORDER BY c.name ASC"
        cur.execute(sql, (name_prefix + "%",))
    else:
        sql = base_sql + "GROUP BY c.id ORDER BY c.name ASC"
        cur.execute(sql)

    return [_row_to_dict(cur, row) for row in cur]


# ---------------------------------------------------------------------------
# Message queries
# ---------------------------------------------------------------------------


def query_messages(
    conn: apsw.Connection,
    channel_name: str,
    limit: int = 50,
    since: str | None = None,
    order: str = "desc",
) -> list[dict[str, object]] | dict[str, str]:
    """Return messages for a given channel with optional filtering.

    If the channel does not exist an error dict is returned instead of a
    message list.

    Args:
        conn: An apsw connection to the Agora database.
        channel_name: The channel name to query (must exist).
        limit: Maximum number of messages to return (default 50).
        since: If provided, only return messages created at or after this
            timestamp string (ISO-8601 compatible).
        order: ``"asc"`` for oldest-first, ``"desc"`` (default) for
            newest-first.

    Returns:
        A list of message dicts on success, or an error dict with keys
        ``error``, ``message``, and ``available_channels`` if the channel
        is not found.

    """
    cur = conn.cursor()

    # --- Check channel exists ---------------------------------------------------
    cur.execute("SELECT id, name FROM chat_channels WHERE name = ?", (channel_name,))
    channel_row = cur.fetchone()

    if channel_row is None:
        # Collect available channel names for the error message
        cur.execute("SELECT name FROM chat_channels ORDER BY name")
        available = [row[0] for row in cur]
        return {
            "error": "CHANNEL_NOT_FOUND",
            "message": f"Channel '{channel_name}' not found",
            "available_channels": available,
        }

    # --- Build query ------------------------------------------------------------
    order_sql = "DESC" if order.lower() == "desc" else "ASC"

    sql = (
        "SELECT m.*, c.name AS channel_name "
        "FROM chat_messages m "
        "JOIN chat_channels c ON m.channel_id = c.id "
        "WHERE c.name = ? "
    )
    params: list[object] = [channel_name]

    if since is not None:
        sql += "AND m.created_at >= ? "
        params.append(since)

    sql += f"ORDER BY m.created_at {order_sql} LIMIT ?"
    params.append(limit)

    cur.execute(sql, tuple(params))
    return [_row_to_dict(cur, row) for row in cur]


# ---------------------------------------------------------------------------
# Aggregate queries
# ---------------------------------------------------------------------------


def get_message_count(conn: apsw.Connection, channel_id: str) -> int:
    """Return the total number of messages in a channel.

    Args:
        conn: An apsw connection to the Agora database.
        channel_id: The UUID of the channel to count messages for.

    Returns:
        The number of messages as an integer.

    """
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM chat_messages WHERE channel_id = ?", (channel_id,))
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Textual TUI application
# ---------------------------------------------------------------------------

# Default database path (relative to working directory)
_DEFAULT_DB_PATH = "agora.db"

# Status color mapping
_STATUS_COLORS: dict[str, str] = {
    "online": "green",
    "offline": "red",
    "busy": "yellow",
}


class AgoraAdmin(App):
    """A Textual TUI for inspecting the Agora database.

    Provides a tree-based navigation pane on the left and a detail pane on
    the right.  Selecting nodes in the tree populates the detail pane with
    agents, channels, or messages.  A footer bar shows status and provides
    a client-side filter input.
    """

    CSS = """
    Screen {
        layout: horizontal;
    }
    #nav-pane {
        width: 25%;
        min-width: 20;
        dock: left;
        border: solid $primary;
        height: 1fr;
    }
    #detail-pane {
        width: 1fr;
        border: solid $primary;
        height: 1fr;
    }
    DataTable {
        height: 1fr;
    }
    #footer-bar {
        height: 3;
        dock: bottom;
        background: $surface;
    }
    #status-label {
        width: 1fr;
        padding: 0 1;
    }
    #filter-input {
        width: 40;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+f", "focus_filter", "Filter"),
        Binding("/", "focus_filter", "Filter"),
        Binding("escape", "clear_filter", "Clear Filter"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        """Initialise the admin app.

        Args:
            db_path: Path to the SQLite database file.

        """
        super().__init__()
        self.db_path = db_path
        self.conn: apsw.Connection | None = None
        self.agents: list[dict[str, object]] = []
        self.channels: list[dict[str, object]] = []
        self._current_view: str = "agents"
        self._current_channel: str | None = None

    def compose(self) -> ComposeResult:
        """Build the UI layout.

        Yields:
            Tree, DataTable, and a footer with status label and filter input.

        """
        with Horizontal(id="nav-pane"):
            yield Tree("Agora Admin", id="nav-tree")
        with Vertical(id="detail-pane"):
            yield DataTable(id="detail-table")
        with Horizontal(id="footer-bar"):
            yield Label("", id="status-label")
            yield Input(placeholder="Filter...", id="filter-input")

    def on_mount(self) -> None:
        """Set up the database connection and populate the tree."""
        try:
            self.conn = apsw.Connection(self.db_path)
        except apsw.Error:
            self._show_error(f"Could not open database: {self.db_path}")
            return

        missing = verify_schema(self.conn)
        if missing:
            self._show_error(
                f"Schema incomplete - missing tables: {', '.join(missing)}",
            )
            return

        self._load_data()
        self._populate_tree()
        self._show_agents_view()

    def _load_data(self) -> None:
        """Reload agents and channels from the database."""
        if self.conn is None:
            return
        self.agents = query_agents(self.conn)
        self.channels = query_channels(self.conn)

    def _populate_tree(self) -> None:
        """Build the navigation tree nodes."""
        tree = self.query_one("#nav-tree", Tree)
        tree.clear()

        agents_node = tree.root.add_leaf(f"Agents ({len(self.agents)})")
        agents_node.data = "agents"

        channels_parent = tree.root.add(f"Channels ({len(self.channels)})")
        channels_parent.data = "channels"

        for ch in self.channels:
            name = ch.get("name", "?")
            count = ch.get("message_count", 0)
            child = channels_parent.add_leaf(f"#{name} ({count} msgs)")
            child.data = f"channel:{name}"

        tree.root.expand()

    def _show_agents_view(self) -> None:
        """Populate the DataTable with agent data."""
        self._current_view = "agents"
        self._current_channel = None
        table = self.query_one("#detail-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "ID",
            "Name",
            "Role",
            "Status",
            "Capabilities",
            "Current Task",
            "Last Heartbeat",
            "Registered",
        )
        for agent in self.agents:
            status = str(agent.get("status", "-"))
            status_style = _STATUS_COLORS.get(status.lower(), "white")
            table.add_row(
                str(agent.get("id", "-")),
                str(agent.get("name", "-")),
                str(agent.get("role", "-")),
                Text(status, style=status_style),
                str(agent.get("capabilities", "-")),
                str(agent.get("current_task", "-")),
                str(agent.get("last_heartbeat_at", "-")),
                str(agent.get("registered_at", "-")),
            )
        self._update_status()

    def _show_channels_view(self) -> None:
        """Populate the DataTable with channel overview."""
        self._current_view = "channels"
        self._current_channel = None
        table = self.query_one("#detail-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Messages", "Last Activity", "Created")
        for ch in self.channels:
            table.add_row(
                str(ch.get("name", "-")),
                str(ch.get("message_count", 0)),
                str(ch.get("last_activity_at", "-")),
                str(ch.get("created_at", "-")),
            )
        self._update_status()

    def _show_channel_messages(self, channel_name: str) -> None:
        """Populate the DataTable with messages from a specific channel.

        Args:
            channel_name: The channel name to display messages for.

        """
        if self.conn is None:
            return
        self._current_view = "messages"
        self._current_channel = channel_name
        table = self.query_one("#detail-table", DataTable)
        table.clear(columns=True)
        table.add_column("Time", width=20)
        table.add_column("Agent", width=15)
        table.add_column("Content", ratio=2)

        result = query_messages(self.conn, channel_name, limit=100)
        if isinstance(result, dict) and "error" in result:
            table.add_row("-", "-", result.get("message", "Error"))
        else:
            for msg in result:  # type: ignore[union-attr]
                table.add_row(
                    str(msg.get("created_at", "-")),
                    str(msg.get("agent_id", "-")),
                    str(msg.get("content", "-")),
                )
        self._update_status()

    def _update_status(self) -> None:
        """Update the footer status label with current counts."""
        label = self.query_one("#status-label", Label)
        label.update(
            f"Agents: {len(self.agents)} | Channels: {len(self.channels)}",
        )

    def _show_error(self, message: str) -> None:
        """Show a full-screen error message.

        Args:
            message: The error text to display.

        """
        self.mount(Static(message, id="error-msg"))

    # -- Tree navigation -----------------------------------------------------

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle tree node selection.

        Args:
            event: The tree node selection event.

        """
        data = event.node.data
        if data == "agents":
            self._show_agents_view()
        elif data == "channels":
            self._show_channels_view()
        elif isinstance(data, str) and data.startswith("channel:"):
            channel_name = data.split(":", 1)[1]
            self._show_channel_messages(channel_name)

    # -- Keybindings ---------------------------------------------------------

    def action_focus_filter(self) -> None:
        """Focus the filter input field."""
        self.query_one("#filter-input", Input).focus()

    def action_clear_filter(self) -> None:
        """Clear the filter input and reset all DataTable row visibility."""
        filter_input = self.query_one("#filter-input", Input)
        filter_input.value = ""
        table = self.query_one("#detail-table", DataTable)
        for row_key in table.rows:
            table.set_row_display(row_key, display=True)
        # Return focus to the tree
        self.query_one("#nav-tree", Tree).focus()

    def action_refresh(self) -> None:
        """Refresh all data from the database."""
        if self.conn is None:
            return
        self._load_data()
        self._populate_tree()
        # Re-show the current view
        if self._current_view == "agents":
            self._show_agents_view()
        elif self._current_view == "channels":
            self._show_channels_view()
        elif self._current_view == "messages" and self._current_channel:
            self._show_channel_messages(self._current_channel)

    # -- Client-side filtering -----------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter DataTable rows based on input text.

        Args:
            event: The input changed event from the filter input.

        """
        if event.input.id != "filter-input":
            return
        query = event.value.lower()
        table = self.query_one("#detail-table", DataTable)
        for row_key in table.rows:
            row_data = table.get_row(row_key)
            match = not query or any(
                query in str(cell).lower() for cell in row_data
            )
            table.set_row_display(row_key, display=match)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and launch the Agora admin TUI."""
    parser = argparse.ArgumentParser(
        description="Agora database admin TUI — inspect agents, channels, and messages.",
    )
    parser.add_argument(
        "--db",
        default="agora.db",
        help="Path to the Agora SQLite database (default: agora.db)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path

    app = AgoraAdmin(str(db_path))
    app.run()


if __name__ == "__main__":
    main()
