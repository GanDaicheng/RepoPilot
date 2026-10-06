"""Versioned business-table schema independent of LangGraph internals."""

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    repo_path TEXT NOT NULL,
    user_request TEXT NOT NULL,
    test_command TEXT NOT NULL,
    model_profile TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    base_commit TEXT,
    worktree_path TEXT,
    retry_count INTEGER NOT NULL,
    max_retries INTEGER NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tasks_claim_idx
ON tasks(status, created_at, id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    dedupe_key TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_task_cursor_idx
ON events(task_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS events_task_dedupe_idx
ON events(task_id, dedupe_key)
WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    error_type TEXT,
    created_at TEXT NOT NULL
);
"""
