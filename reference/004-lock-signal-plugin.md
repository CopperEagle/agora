# 004 — Lock and Signal Plugin

**Priority:** P1 | **Phase:** NOW | **Dependencies:** 001-backbone-scaffold

## What

The **Lock/Signal plugin** provides mutual exclusion (locks) and event notification (signals) for multi-agent coordination. Locks prevent two agents from working on the same resource. Signals allow one agent to notify another without polling or chat messages.

Tool prefix: `lock_` and `signal_`

## Plugin Interface

```python
class LockSignalPlugin(AgoraPlugin):
    name = "locks"
    version = "1.0.0"
    description = "Mutual exclusion and event notification"

    def get_tools(self):
        return [
            ToolDef("lock_acquire", self.lock_acquire, schema),
            ToolDef("lock_release", self.lock_release, schema),
            ToolDef("lock_status", self.lock_status, schema),
            ToolDef("signal_send", self.signal_send, schema),
            ToolDef("signal_wait", self.signal_wait, schema),
            ToolDef("signal_clear", self.signal_clear, schema),
        ]

    def get_migrations(self):
        return [
            """CREATE TABLE locks (
                id TEXT PRIMARY KEY,
                resource TEXT UNIQUE NOT NULL,
                agent_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );""",
            """CREATE TABLE signals (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                consumed INTEGER DEFAULT 0,
                consumed_at TEXT
            );""",
            """CREATE INDEX idx_signals_name_consumed
                ON signals(name, consumed, created_at);""",
        ]
```

## Tools

### Lock Tools

1. `lock_acquire(resource, timeout=30, ttl=300)` → lock_id | null
   - Acquires an exclusive lock on a named resource
   - Polls up to `timeout` seconds; returns null if can't acquire
   - Lock auto-expires after `ttl` seconds (prevents deadlocks)
   - Returns lock_id that must be presented on release

2. `lock_release(lock_id)` → success/failure
   - Releases the lock
   - Fails if lock_id doesn't match or already expired

3. `lock_status(resource)` → { holder, acquired_at, expires_at } | null

### Signal Tools

4. `signal_send(signal_name, payload?)` → signal_id
   - Creates a named signal with optional JSON payload
   - Multiple consumers can observe the same signal
   - Emits: `signal.sent`

5. `signal_wait(signal_name, timeout=60)` → signal | null
   - Waits for a new signal with the given name
   - Returns null on timeout
   - Returns the "oldest unconsumed" signal (FIFO)

6. `signal_clear(signal_name)` → count
   - Clears all unconsumed signals with the given name
   - For cleanup when a coordination sequence resets

## Events

**Emits:** `signal.sent` (payload: signal_name, agent_id)

**Consumes:** (none — this is a utility plugin, used by other plugins via its tools, not events)

## Why This Matters

Locks and signals solve the two hardest problems in multi-agent coordination:

- **Locks**: Two agents editing the same file → corruption. Locks serialize access.
- **Signals**: Agent A needs Agent B's work to continue. Rather than polling, Agent A calls `signal_wait("report.done")` and Agent B calls `signal_send("report.done")`.

Without these, agents fall back to chat-based coordination — slow, race-prone, and hard to debug.

## Technical Notes

- Lock TTL is essential — agents crash or lose context. Expired locks are reclaimed automatically.
- Signals are one-shot (consumed on read). For persistent state, use the Board plugin.
- For the initial version, polling (not SSE) is fine — cloud-model context windows make polling cheap.
- Other plugins can use Lock/Signal via the event bus or by calling its tools directly (if the backbone allows cross-plugin tool calls).

## Relevant Context

- **ARCHITECTURE.md** — plugin system
- The lifecycle pattern: Agent A locks `src/main.py`, Agent B waits for `signal_wait("review:main.py.done")`, B can then safely work
- **CAID** (arXiv:2603.21489): isolated worktrees + structured merge validates this pattern
- **PRISM** (arXiv:2602.01532): two-signal decomposition — "ready to act" + "action taken"
