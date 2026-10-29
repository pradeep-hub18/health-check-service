from unittest.mock import MagicMock

import pytest

from app import create_app


@pytest.fixture
def client():
    app = create_app(initialize_database=False)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def db(monkeypatch):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    monkeypatch.setattr("app.routes.get_connection", lambda: conn)
    return conn
