-- agent.db (Postgres dialect): the agent's memory, also its job queue.
CREATE TABLE IF NOT EXISTS thread (
    id         TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

CREATE TABLE IF NOT EXISTS message (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id  TEXT NOT NULL REFERENCES thread (id),
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'model')),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    UNIQUE (thread_id, seq)
);

CREATE OR REPLACE FUNCTION message_no_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'message is append-only: UPDATE rejected';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION message_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'message is append-only: DELETE rejected';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS message_no_update ON message;
CREATE TRIGGER message_no_update BEFORE UPDATE ON message
    FOR EACH ROW EXECUTE FUNCTION message_no_update();

DROP TRIGGER IF EXISTS message_no_delete ON message;
CREATE TRIGGER message_no_delete BEFORE DELETE ON message
    FOR EACH ROW EXECUTE FUNCTION message_no_delete();

CREATE TABLE IF NOT EXISTS run (
    id               TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL REFERENCES thread (id),
    status           TEXT NOT NULL CHECK (status IN
                           ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead')),
    model            TEXT NOT NULL,
    tokens_in        INTEGER NOT NULL DEFAULT 0,
    tokens_out       INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    available_at     DOUBLE PRECISION NOT NULL,
    lease_owner      TEXT,
    lease_until      DOUBLE PRECISION,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    error_code       TEXT,
    created_at       TEXT NOT NULL DEFAULT (to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    started_at       TEXT,
    finished_at      TEXT
);

CREATE INDEX IF NOT EXISTS run_claimable ON run (status, available_at);

CREATE TABLE IF NOT EXISTS run_step (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES run (id),
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('model', 'tool')),
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    text       TEXT,
    tool_calls TEXT,
    created_at TEXT NOT NULL DEFAULT (to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_call (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_step_id     BIGINT NOT NULL UNIQUE REFERENCES run_step (id),
    tool_name       TEXT NOT NULL,
    args            TEXT NOT NULL,
    result          TEXT NOT NULL,
    ok              INTEGER NOT NULL CHECK (ok IN (0, 1)),
    latency_ms      INTEGER NOT NULL,
    idempotency_key TEXT,
    created_at      TEXT NOT NULL DEFAULT (to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);