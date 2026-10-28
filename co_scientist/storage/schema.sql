-- AI Co-Scientist — SQLite schema (initial)
-- Apply via co_scientist.storage.db.init_db / migrate.
-- WAL is set at connection time, not here.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    status              TEXT NOT NULL,                 -- running|paused|done|failed|aborted
    research_goal       TEXT NOT NULL,
    research_plan       TEXT NOT NULL,                 -- JSON
    config_snapshot     TEXT NOT NULL,                 -- JSON (frozen config at start)
    budget_tokens       INTEGER NOT NULL,
    budget_usd          REAL NOT NULL,
    budget_used_tokens  INTEGER NOT NULL DEFAULT 0,
    budget_used_usd     REAL NOT NULL DEFAULT 0,
    wall_deadline       TEXT,
    final_overview      TEXT
);
CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS hypotheses (
    id              TEXT PRIMARY KEY,                  -- deterministic sha256 prefix
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,                     -- generation|evolution
    strategy        TEXT NOT NULL,                     -- literature|debate|combine|simplify|out_of_box|feasibility
    parent_ids      TEXT,                              -- JSON array
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    full_text       TEXT NOT NULL,
    artifact_path   TEXT NOT NULL,
    elo             REAL,
    matches_played  INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL,                     -- draft|reviewed|in_tournament|pinned|rejected|quarantined|retired
    dedup_cluster   TEXT
);
CREATE INDEX IF NOT EXISTS hyp_sess_elo   ON hypotheses(session_id, elo DESC);
CREATE INDEX IF NOT EXISTS hyp_sess_state ON hypotheses(session_id, state);

CREATE TABLE IF NOT EXISTS reviews (
    id              TEXT PRIMARY KEY,                  -- sha256(hyp_id || kind || iteration)
    hypothesis_id   TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,                     -- full|verification|observation|simulation
    verdict         TEXT,                              -- already_explained|other_more_likely|missing_piece|neutral|disproved
    novelty         REAL,
    correctness     REAL,
    testability     REAL,
    feasibility     REAL,
    body            TEXT NOT NULL,
    artifact_path   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rev_hyp ON reviews(hypothesis_id, created_at DESC);
CREATE INDEX IF NOT EXISTS rev_sess ON reviews(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tournament_matches (
    id              TEXT PRIMARY KEY,                  -- sha256(min(a,b) || max(a,b) || round_id)
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    hyp_a           TEXT NOT NULL REFERENCES hypotheses(id),
    hyp_b           TEXT NOT NULL REFERENCES hypotheses(id),
    mode            TEXT NOT NULL,                     -- pairwise|debate|batch|invalid
    winner          TEXT,                              -- 'a'|'b'|NULL (invalid)
    elo_a_before    REAL NOT NULL,
    elo_b_before    REAL NOT NULL,
    elo_a_after     REAL,
    elo_b_after     REAL,
    rationale       TEXT,
    transcript_id   TEXT,
    similarity      REAL
);
CREATE INDEX IF NOT EXISTS mat_sess ON tournament_matches(session_id, created_at DESC);

-- Append-only Elo ledger. UNIQUE on match_id makes Elo updates idempotent.
CREATE TABLE IF NOT EXISTS elo_journal (
    update_id       TEXT PRIMARY KEY,                  -- = match.id
    match_id        TEXT UNIQUE NOT NULL,
    hyp_a           TEXT NOT NULL,
    hyp_b           TEXT NOT NULL,
    winner          TEXT NOT NULL,
    elo_a_before    REAL NOT NULL,
    elo_b_before    REAL NOT NULL,
    elo_a_after     REAL NOT NULL,
    elo_b_after     REAL NOT NULL,
    applied_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    agent               TEXT NOT NULL,
    action              TEXT NOT NULL,
    target_id           TEXT,
    payload             TEXT NOT NULL,                 -- JSON
    priority            INTEGER NOT NULL DEFAULT 100,
    status              TEXT NOT NULL,                 -- pending|leased|in_progress|done|failed|dead|cancelled
    lease_owner         TEXT,
    lease_expires_at    INTEGER,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    idempotency_key     TEXT UNIQUE,
    approval_sha256     TEXT,
    result_json         TEXT
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(session_id, status, priority, created_at);

CREATE TABLE IF NOT EXISTS transcripts (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_id         TEXT REFERENCES tasks(id),
    agent           TEXT NOT NULL,
    action          TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    cache_write     INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    artifact_path   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trn_sess ON transcripts(session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS trn_task ON transcripts(task_id);

CREATE TABLE IF NOT EXISTS system_feedback (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    source          TEXT NOT NULL,                     -- human|meta_review
    kind            TEXT NOT NULL,                     -- directive|preference|rejection|pin|system_feedback
    target_id       TEXT,
    text            TEXT NOT NULL,
    artifact_path   TEXT,
    active          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS fb_sess_active ON system_feedback(session_id, active, created_at DESC);

CREATE TABLE IF NOT EXISTS embeddings_meta (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    hypothesis_id   TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    dim             INTEGER NOT NULL,
    faiss_offset    INTEGER NOT NULL,
    text_hash       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(hypothesis_id, model)
);
CREATE INDEX IF NOT EXISTS emb_sess ON embeddings_meta(session_id);

-- Observability
CREATE TABLE IF NOT EXISTS spans (
    span_id         TEXT PRIMARY KEY,
    trace_id        TEXT NOT NULL,
    parent_span_id  TEXT,
    session_id      TEXT,
    task_id         TEXT,
    name            TEXT NOT NULL,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    attrs_json      TEXT,
    status          TEXT                                -- ok|error|unset
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS spans_task  ON spans(task_id);
CREATE INDEX IF NOT EXISTS spans_sess  ON spans(session_id, started_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    session_id      TEXT,
    task_id         TEXT,
    agent           TEXT,
    event           TEXT NOT NULL,
    payload         TEXT                                -- JSON
);
CREATE INDEX IF NOT EXISTS events_sess ON events(session_id, ts DESC);
CREATE INDEX IF NOT EXISTS events_type ON events(event, ts DESC);

-- Scientific Evidence Layer. Values remain linked to original tool artifacts.
CREATE TABLE IF NOT EXISTS scientific_evidence (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    hypothesis_id TEXT REFERENCES hypotheses(id) ON DELETE SET NULL,
    design_id TEXT,
    design_iteration INTEGER,
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
    evaluation_condition TEXT,
    notes TEXT,
    parent_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_hyp ON scientific_evidence(hypothesis_id, created_at);
CREATE INDEX IF NOT EXISTS evidence_task ON scientific_evidence(task_id, created_at);

-- Schema version tracking (linear migrations)
-- Pre-research drafts are independent of formal scientific Sessions.
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
    busy_until REAL,
    limits_json TEXT NOT NULL DEFAULT '{}'
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
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    UNIQUE(draft_id, request_id)
);
CREATE INDEX IF NOT EXISTS scoping_calls_draft ON scoping_calls(draft_id, created_at);

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

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    name        TEXT NOT NULL
);
