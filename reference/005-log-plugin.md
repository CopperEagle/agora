# 005 — Log Plugin

**Priority:** P1 | **Phase:** NOW | **Dependencies:** 001-backbone-scaffold

## What

The **Log plugin** provides comprehensive activity recording — every tool call across all plugins is automatically logged with timestamps, token counts, and outcomes. This is the data source for cost analysis, meta-improvement, and debugging.

Key difference from a monolithic log: the backbone **automatically emits** `tool.executed` events after every tool call. The Log plugin subscribes to this event and handles persistence. No other plugin needs to instrument logging.

Tool prefix: `log_`

## Plugin Interface

```python
class LogPlugin(AgoraPlugin):
    name = "log"
    version = "1.0.0"
    description = "Activity tracking, cost analysis, and failure capture"

    def on_load(self, config):
        self.retention_days = config.get("retention_days", 90)

    def on_startup(self):
        # Start archival sweep thread (for old log entries)
        pass

    def get_tools(self):
        return [
            ToolDef("log_query", self.query_log, schema),
            ToolDef("log_costs", self.get_costs, schema),
            ToolDef("log_summary", self.get_summary, schema),
            ToolDef("log_report_failure", self.report_failure, schema),
            ToolDef("log_project_cost", self.project_cost, schema),
        ]
```

## Database Migrations

```sql
CREATE TABLE activity_log (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    model TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    outcome TEXT        -- 'success' | 'failure' | 'error'
);

CREATE TABLE failure_log (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session_id TEXT,
    tool TEXT NOT NULL,
    goal TEXT,                    -- what was being attempted
    error TEXT,                   -- what went wrong
    recovery TEXT,                -- how it was recovered
    root_cause TEXT,              -- categorized: timing | context_loss | tool_error | reasoning_error | unknown
    created_at TEXT NOT NULL
);

CREATE INDEX idx_activity_agent_time
    ON activity_log(agent_id, started_at);
CREATE INDEX idx_activity_tool_time
    ON activity_log(tool, started_at);
CREATE INDEX idx_failure_agent
    ON failure_log(agent_id);
```

## Tools

1. `log_query(filter)` → entries[]
   - Filter by agent_id, tool, time range, outcome
   - Sortable, paginated (limit/offset)

2. `log_costs(since?, agent_id?)` → cost breakdown
   - Per-agent total token usage and estimated cost
   - Per-tool token usage
   - Per-session token usage
   - Daily/weekly/monthly totals

3. `log_summary(since?)` → activity summary
   - Call volume (total, per plugin, per agent)
   - Error rate
   - Busiest agents and tools

4. `log_report_failure(goal, error, recovery?, root_cause?)` → failure_id
   - Structured failure capture for the failure library
   - This feeds into meta-improvement

5. `log_project_cost(agent_id?, daily_token_target?)` → estimated monthly cost
   - "If current rate continues, expected monthly cost is $X"

## Event Consumption

The Log plugin subscribes to the backbone's `tool.executed` event. Every tool call across all plugins is automatically recorded:

```python
def on_tool_executed(self, event):
    # event.payload contains: agent_id, session_id, tool, input_tokens,
    #                        output_tokens, model, started_at, completed_at,
    #                        duration_ms, outcome
    self.activity_log.insert(event.payload)
```

This means the Chat, Board, and Lock/Signal plugins **don't need to add any logging code**. The backbone emits the event, the Log plugin persists it. Other plugins are fully decoupled from logging.

## Why This Matters

Without data, meta-improvement is wishful thinking. The Log plugin provides:

- **Cost awareness**: Know exactly how many tokens each agent uses. Detect runaway agents.
- **Debugging**: When coordination fails, query the log to see exactly what each agent did and when.
- **Meta-improvement**: The `meta-improvement/` project uses this data to detect failure patterns, propose improvements, and evaluate interventions.
- **Incident response**: "What happened?" — query the log around the time of the incident.
- **PRISM calibration**: Historical data on when proactive actions succeeded vs. failed.

## Technical Notes

- Logs are append-only (no edits, no deletes)
- Token counting: Agents report their own token usage; the backbone passes it through
- Cost estimation uses configurable rates: `$per_million_input_tokens`, `$per_million_output_tokens`
- Log retention: Configurable (default 90 days). Archive (don't delete) old entries.
- The failure log feeds directly into the meta-improvement project's failure library
- The Log plugin itself is recorded in the activity log (via the backbone event) — infinite regress is avoided by the backbone skipping the Log event for Log's own tool calls

## Relevant Context

- **ARCHITECTURE.md** — backbone event bus, plugin decoupling
- **META.md** — vision
- **Meta-Improvement META.md** — this log is the primary input for the improvement cycle
- **projectmem** (arXiv:2606.12329): Structured failure reflection is essential
