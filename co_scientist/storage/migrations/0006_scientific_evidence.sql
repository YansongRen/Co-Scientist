-- Scientific Evidence Layer: append-only provenance for computational and
-- experimental evidence consumed by agents.
CREATE TABLE IF NOT EXISTS scientific_evidence (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    hypothesis_id TEXT REFERENCES hypotheses(id) ON DELETE SET NULL,
    design_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value TEXT,
    unit TEXT,
    uncertainty TEXT,
    model_version TEXT,
    config_sha256 TEXT,
    source_path TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    source_role TEXT NOT NULL,
    notes TEXT,
    parent_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_hyp ON scientific_evidence(hypothesis_id, created_at);
CREATE INDEX IF NOT EXISTS evidence_task ON scientific_evidence(task_id, created_at);
