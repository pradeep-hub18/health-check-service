"""Every test owns a temporary PostgreSQL schema; never truncate shared tables."""
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from app import create_app
from app.database import init_db


@pytest.fixture
def database(monkeypatch):
    dsn = os.getenv("TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_DSN to an explicitly disposable PostgreSQL database")
    schema = "healthcheck_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connection():
        return psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=3,
            options=f"-c search_path={schema} -c statement_timeout=3000 -c lock_timeout=2000",
        )

    for module in ("app.database", "app.routes", "app.checker", "app.scheduler"):
        monkeypatch.setattr(module + ".get_connection", connection)
    try:
        init_db()
        yield connection
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def integration_client(database):
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def http_target(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_ENDPOINTS", "true")
    state = {"status": 200, "disconnect": False, "entered": None, "release": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if state["entered"] is not None:
                state["entered"].set()
                state["release"].wait(timeout=3)
            if state["disconnect"]:
                self.close_connection = True
                return
            self.send_response(state["status"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
