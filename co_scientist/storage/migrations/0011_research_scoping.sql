CREATE TABLE IF NOT EXISTS scoping_drafts (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    messages_json TEXT NOT NULL DEFAULT '[]',
    brief_json TEXT,
    use_live INTEGER NOT NULL DEFAULT 0,
    linked_session_id TEXT UNIQUE REFERENCES sessions(id),
    busy_call_id TEXT,
    busy_until REAL
);
CREATE TABLE IF NOT EXISTS scoping_calls (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES scoping_drafts(id),
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_json TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    reserved_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_usd REAL NOT NULL DEFAULT 0,
    usage_known INTEGER NOT NULL DEFAULT 0,
    transcript_json TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(draft_id, request_id)
);
CREATE INDEX IF NOT EXISTS scoping_calls_draft ON scoping_calls(draft_id, created_at);
