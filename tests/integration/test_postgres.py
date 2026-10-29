import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app.checker import check_endpoint
from app.scheduler import HealthCheckScheduler

pytestmark = pytest.mark.integration


def register(client, url):
    response = client.post('/endpoints', json={'url': url, 'interval_seconds': 30})
    assert response.status_code == 201
    return response.json['id']


def test_registration_duplicate_list_and_delete_cascade(integration_client, database, http_target):
    client = integration_client
    url, state = http_target
    eid = register(client, url)
    assert client.post('/endpoints', json={'url': url, 'interval_seconds': 30}).status_code == 409
    assert client.get('/endpoints').json[0]['id'] == eid
    assert check_endpoint(eid)[0]['success'] is True
    assert client.get(f'/endpoints/{eid}/history').json['count'] == 1
    assert client.delete(f'/endpoints/{eid}').status_code == 200
    assert client.delete(f'/endpoints/{eid}').status_code == 404
    assert client.get(f'/endpoints/{eid}/history').status_code == 404
    with database() as conn:
        assert conn.execute('SELECT count(*) AS n FROM check_results').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM state_transitions').fetchone()['n'] == 0


def test_real_http_success_failure_recovery_and_transitions(integration_client, http_target):
    url, state = http_target
    eid = register(integration_client, url)
    for status in (200,200,500,500,200):
        state['status'] = status
        result, code = check_endpoint(eid)
        assert code == 200
        assert result['success'] == (status == 200)
    history = integration_client.get(f'/endpoints/{eid}/history').json
    assert history['count'] == 5
    transitions = integration_client.get(f'/endpoints/{eid}/transitions').json['transitions']
    assert [(r['from_state'],r['to_state']) for r in reversed(transitions)] == [
        ('unknown','healthy'), ('healthy','unhealthy'), ('unhealthy','healthy')]


def test_request_failure_is_persisted(integration_client, http_target):
    url, state = http_target
    eid = register(integration_client, url)
    state['disconnect'] = True
    result, code = check_endpoint(eid)
    assert code == 200
    assert result['success'] is False
    history = integration_client.get(f'/endpoints/{eid}/history').json['history'][0]
    assert history['actual_status_code'] is None
    assert history['error_message']
    assert history['success'] is False


def test_timestamp_filters_order_and_limit(integration_client, database):
    eid = register(integration_client, 'https://example.com')
    with database() as conn:
        conn.execute('''INSERT INTO check_results(endpoint_id, success, checked_at)
            SELECT %s, true, '2026-01-01T00:00:00Z'::timestamptz + n * INTERVAL '1 second'
            FROM generate_series(0, 104) AS n''', (eid,))
    data = integration_client.get(f'/endpoints/{eid}/history').json
    assert data['count'] == 100
    assert data['history'][0]['id'] > data['history'][-1]['id']
    response = integration_client.get(f'/endpoints/{eid}/history?from=2026-01-01T00:00:10Z&to=2026-01-01T00:00:12Z')
    assert response.status_code == 200
    assert response.json['count'] == 3


def test_database_failure_rolls_back_result_and_state(integration_client, database, http_target, monkeypatch):
    url, state = http_target
    eid = register(integration_client, url)
    class FailingConnection:
        def __init__(self):
            self.conn = database()
        def __enter__(self):
            self.conn.__enter__()
            return self
        def __exit__(self, *args):
            return self.conn.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def execute(self, query, params=None):
            if 'INSERT INTO state_transitions' in query:
                raise psycopg.OperationalError('injected write failure')
            return self.conn.execute(query, params)
    monkeypatch.setattr('app.checker.get_connection', FailingConnection)
    with pytest.raises(psycopg.OperationalError):
        check_endpoint(eid)
    with database() as conn:
        assert conn.execute('SELECT count(*) AS n FROM check_results').fetchone()['n'] == 0
        assert conn.execute('SELECT current_state FROM endpoints WHERE id=%s',(eid,)).fetchone()['current_state'] == 'unknown'


def test_deletion_during_request_does_not_resurrect_endpoint(integration_client, database, http_target):
    url, state = http_target
    eid = register(integration_client, url)
    state['entered'] = threading.Event()
    state['release'] = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(check_endpoint, eid)
        try:
            assert state['entered'].wait(timeout=2)
            assert integration_client.delete(f'/endpoints/{eid}').status_code == 200
        finally:
            state['release'].set()
        assert future.result(timeout=3)[1] == 404
    with database() as conn:
        assert conn.execute('SELECT count(*) AS n FROM check_results').fetchone()['n'] == 0


def test_scheduler_persists_and_stops(integration_client, http_target):
    eid = register(integration_client, http_target[0])
    scheduler = HealthCheckScheduler(max_workers=2, poll_interval=.02)
    thread = threading.Thread(target=scheduler.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            if integration_client.get(f'/endpoints/{eid}/history').json['count']:
                break
            time.sleep(.02)
        assert integration_client.get(f'/endpoints/{eid}/history').json['count'] == 1
        assert scheduler.healthy()
    finally:
        scheduler.stop()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert not scheduler.in_flight
