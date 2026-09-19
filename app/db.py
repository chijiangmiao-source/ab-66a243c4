"""PostgreSQL connection management and schema management.

All persistence lives in one database. The schema is created/upgraded
idempotently on service and worker startup.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS fencing_generation_seq;

CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version INTEGER NOT NULL
);

-- Immutable problem instances (the canonical normalized JSON).
CREATE TABLE IF NOT EXISTS problems (
    problem_hash   CHAR(64) PRIMARY KEY,
    problem_json   JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotency keys map 1:1 to a problem hash. A second problem with the
-- same key is a conflict (HTTP 409).
CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key  TEXT PRIMARY KEY,
    job_id           UUID NOT NULL,
    problem_hash     CHAR(64) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id               UUID PRIMARY KEY,
    problem_hash     CHAR(64) NOT NULL REFERENCES problems,
    status           TEXT NOT NULL CHECK (status IN
                       ('queued','running','succeeded','infeasible','cancelled')),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,

    -- Lease / fencing
    generation       BIGINT NOT NULL DEFAULT 0,
    worker_id        TEXT,
    lease_expires_at TIMESTAMPTZ,

    -- Persisted progress
    checkpoint       JSONB,
    nodes_explored   BIGINT NOT NULL DEFAULT 0,
    frontier_size    INTEGER NOT NULL DEFAULT 0,
    best_size        INTEGER,
    best_risk        BIGINT,
    best_ids         JSONB,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    result           JSONB,
    error            TEXT,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx
    ON jobs (status, lease_expires_at);

INSERT INTO schema_meta (id, version)
VALUES (1, %s)
ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version;
"""


def dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@db:5432/panels",
    )


def connect(autocommit: bool = False):
    conn = psycopg.connect(dsn(), row_factory=dict_row, autocommit=autocommit)
    return conn


@contextmanager
def tx(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_schema() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL, (SCHEMA_VERSION,))
        conn.commit()
