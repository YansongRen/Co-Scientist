ALTER TABLE scoping_drafts ADD COLUMN limits_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE scoping_calls ADD COLUMN elapsed_seconds REAL NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS input_groups (
    id TEXT PRIMARY KEY,
    draft_id TEXT UNIQUE REFERENCES scoping_drafts(id),
    session_id TEXT UNIQUE REFERENCES sessions(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS input_files (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES input_groups(id),
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    extraction_truncated INTEGER NOT NULL DEFAULT 0,
    parse_status TEXT NOT NULL,
    parse_error TEXT,
    width INTEGER,
    height INTEGER,
    created_at TEXT NOT NULL,
    removed_at TEXT,
    UNIQUE(group_id,sha256)
);
CREATE INDEX IF NOT EXISTS input_files_group ON input_files(group_id,created_at);
