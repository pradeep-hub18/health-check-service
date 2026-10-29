from datetime import datetime, timezone
from unittest.mock import Mock

import psycopg
import pytest


def test_liveness_and_home_do_not_require_database(client):
    assert client.get('/healthz').json == {"status": "ok"}
    assert client.get('/').json["service"] == "health-check-service"


@pytest.mark.parametrize('body', [None, [], 'text', {},
    {"url": "https://example.com", "interval_seconds": True},
    {"url": "https://example.com", "interval_seconds": 0},
    {"url": "https://example.com", "interval_seconds": 2**31},
    {"url": "https://example.com", "interval_seconds": 1.5},
    {"url": "https://example.com", "interval_seconds": 30, "expected_status_code": True},
    {"url": "https://example.com", "interval_seconds": 30, "expected_status_code": 600},
    {"url": "https://", "interval_seconds": 30},
    {"url": "https://example.com/\ud800", "interval_seconds": 30},
    {"url": "http://127.0.0.1", "interval_seconds": 30},
])
def test_invalid_registration_without_database(client, monkeypatch, body):
    monkeypatch.delenv('ALLOW_PRIVATE_ENDPOINTS', raising=False)
    assert client.post('/endpoints', json=body).status_code == 400


def test_body_limit(client):
    assert client.post('/endpoints', data='x'*17000, content_type='application/json').status_code == 413


def test_registration(client, db):
    db.execute.return_value.fetchone.return_value = {
        "id": 1, "url": "https://example.com", "interval_seconds": 30,
        "expected_status_code": 200, "current_state": "unknown"}
    response = client.post('/endpoints', json={"url": "https://example.com", "interval_seconds": 30})
    assert response.status_code == 201
    assert response.json['current_state'] == 'unknown'
    assert db.execute.call_args.args[1] == ('https://example.com', 30, 200)
    db.close.assert_called_once()


def test_duplicate_registration(client, db):
    db.execute.side_effect = psycopg.errors.UniqueViolation('duplicate')
    response = client.post('/endpoints', json={"url": "https://example.com", "interval_seconds": 30})
    assert response.status_code == 409
    db.close.assert_called_once()


@pytest.mark.parametrize('method,path', [('get','/endpoints'), ('delete','/endpoints/1'),
    ('get','/endpoints/1/history'), ('get','/endpoints/1/transitions'), ('get','/readyz'),
    ('post','/endpoints')])
def test_database_unavailable_returns_503(client, monkeypatch, method, path):
    def fail():
        raise psycopg.OperationalError('private diagnostic')
    monkeypatch.setattr('app.routes.get_connection', fail)
    response = getattr(client, method)(path, json={"url": "https://example.com", "interval_seconds": 30})
    assert response.status_code == 503
    assert b'private diagnostic' not in response.data


@pytest.mark.parametrize('method,path', [('get','/endpoints'), ('delete','/endpoints/1'),
    ('get','/endpoints/1/history'), ('get','/endpoints/1/transitions')])
def test_query_error_closes_connection(client, db, method, path):
    db.execute.side_effect = psycopg.OperationalError('query error')
    assert getattr(client, method)(path).status_code == 503
    db.close.assert_called_once()


def test_delete_success_and_missing(client, db):
    db.execute.return_value.fetchone.return_value = {"id": 7}
    assert client.delete('/endpoints/7').json['id'] == 7
    db.execute.return_value.fetchone.return_value = None
    assert client.delete('/endpoints/7').status_code == 404


@pytest.mark.parametrize('path', ['history', 'transitions'])
def test_history_missing_endpoint(client, db, path):
    db.execute.return_value.fetchone.return_value = None
    assert client.get('/endpoints/123/'+path).status_code == 404


@pytest.mark.parametrize('query', ['from=garbage', 'from=', 'from=2026-01-01',
    'from=2026-01-02T00:00:00Z&to=2026-01-01T00:00:00Z'])
def test_history_bad_filters(client, query):
    assert client.get('/endpoints/1/history?'+query).status_code == 400


def test_history_filters_use_parameters(client, db):
    db.execute.return_value.fetchone.return_value = {"id": 1}
    db.execute.return_value.fetchall.return_value = []
    response = client.get('/endpoints/1/history?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z')
    assert response.status_code == 200
    query, params = db.execute.call_args.args
    assert params == [1, datetime(2026,1,1,tzinfo=timezone.utc), datetime(2026,1,2,tzinfo=timezone.utc)]
    assert 'LIMIT 100' in query
    assert response.json == {"endpoint_id": 1, "count": 0, "history": []}


def test_readiness_includes_runtime_scheduler(client, db):
    assert client.get('/readyz').status_code == 200
    client.application.extensions['health_check_scheduler'] = Mock(healthy=lambda: False)
    assert client.get('/readyz').status_code == 503


def test_metrics_zero_missing_states_and_survive_db_failure(client, db):
    db.execute.return_value.fetchall.return_value = [{"current_state": "healthy", "total": 2}]
    response = client.get('/metrics')
    assert response.status_code == 200
    assert b'health_check_endpoints{state="healthy"} 2.0' in response.data
    assert b'health_check_endpoints{state="unhealthy"} 0.0' in response.data
    assert b'health_check_database_up 1.0' in response.data
    db.execute.side_effect = psycopg.OperationalError('unavailable')
    response = client.get('/metrics')
    assert response.status_code == 200
    assert b'health_check_database_up 0.0' in response.data
