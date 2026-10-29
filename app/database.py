
import os

import psycopg
from psycopg.rows import dict_row


def get_connection():
    """Create and return a connection to PostgreSQL."""

    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "healthcheck"),
        user=os.getenv("DB_USER", "healthapp"),
        password=os.getenv("DB_PASSWORD"),
        row_factory=dict_row,
        connect_timeout=3,
        options="-c statement_timeout=3000 -c lock_timeout=2000",
    )

def init_db():
    """Create the database tables and indexes if they do not exist."""

    schema = """
    CREATE TABLE IF NOT EXISTS endpoints (
        id BIGSERIAL PRIMARY KEY,
        url TEXT NOT NULL UNIQUE,
        interval_seconds INTEGER NOT NULL
            CHECK (interval_seconds > 0),
        expected_status_code INTEGER NOT NULL DEFAULT 200
            CHECK (expected_status_code BETWEEN 100 AND 599),
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        current_state TEXT NOT NULL DEFAULT 'unknown'
            CHECK (current_state IN ('unknown', 'healthy', 'unhealthy')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS check_results (
        id BIGSERIAL PRIMARY KEY,
        endpoint_id BIGINT NOT NULL
            REFERENCES endpoints(id) ON DELETE CASCADE,
        checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        success BOOLEAN NOT NULL,
        actual_status_code INTEGER,
        response_time_ms DOUBLE PRECISION,
        error_message TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_check_results_endpoint_time
        ON check_results(endpoint_id, checked_at DESC);

    CREATE TABLE IF NOT EXISTS state_transitions (
        id BIGSERIAL PRIMARY KEY,
        endpoint_id BIGINT NOT NULL
            REFERENCES endpoints(id) ON DELETE CASCADE,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        transitioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_transitions_endpoint_time
        ON state_transitions(endpoint_id, transitioned_at DESC);
    """

    with get_connection() as conn:
        conn.execute(schema)
