import threading
from concurrent.futures import Future
from unittest.mock import Mock

import psycopg
import pytest

from app.scheduler import HealthCheckScheduler


class ControlledExecutor:
    def __init__(self, immediate=False):
        self.jobs = []
        self.immediate = immediate
        self.closed = False

    def submit(self, function, endpoint_id):
        future = Future()
        self.jobs.append((endpoint_id, future))
        if self.immediate:
            future.set_result(({}, 200))
        return future

    def shutdown(self, **kwargs):
        self.closed = True


@pytest.fixture
def scheduler():
    scheduler = HealthCheckScheduler(max_workers=2)
    scheduler.executor.shutdown()
    scheduler.executor = ControlledExecutor()
    scheduler._load_endpoints = Mock(return_value=[
        {"id": i, "interval_seconds": 30} for i in range(1, 5)])
    return scheduler


def test_bounds_outstanding_work_and_does_not_overlap(scheduler):
    scheduler._poll_once()
    scheduler._poll_once()
    assert [eid for eid, _ in scheduler.executor.jobs] == [1, 2]
    assert len(scheduler.in_flight) == 2
    scheduler.executor.jobs[0][1].set_result(({},200))
    scheduler._poll_once()
    assert [eid for eid, _ in scheduler.executor.jobs] == [1, 2, 3]


def test_immediately_completed_future_cannot_deadlock(scheduler):
    scheduler.executor = ControlledExecutor(immediate=True)
    thread = threading.Thread(target=scheduler._poll_once, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive(), 'Callback must execute outside scheduler lock'
    assert not scheduler.in_flight
    assert len(scheduler.executor.jobs) == 4


def test_interval_and_fairness(scheduler, monkeypatch):
    now = [100]
    monkeypatch.setattr('app.scheduler.time.monotonic', lambda: now[0])
    scheduler._poll_once()
    for _, future in scheduler.executor.jobs:
        future.set_result(({},200))
    now[0] = 131
    scheduler._poll_once()
    assert [eid for eid, _ in scheduler.executor.jobs] == [1, 2, 3, 4]
    for _, future in scheduler.executor.jobs[2:]:
        future.set_result(({},200))
    scheduler._poll_once()
    assert [eid for eid, _ in scheduler.executor.jobs] == [1, 2, 3, 4, 1, 2]


def test_prunes_deleted_endpoint_bookkeeping(scheduler):
    scheduler.last_check = {99: 1, 100: 1}
    scheduler.in_flight = {100}
    scheduler._poll_once()
    assert 99 not in scheduler.last_check
    assert 100 in scheduler.last_check


def test_submit_failure_releases_capacity_and_allows_retry(scheduler):
    scheduler.executor.submit = Mock(side_effect=RuntimeError('closed'))
    with pytest.raises(RuntimeError):
        scheduler._poll_once()
    assert scheduler.in_flight == set()
    assert scheduler.last_check == {}


def test_failed_check_releases_capacity(scheduler):
    scheduler._poll_once()
    scheduler.executor.jobs[0][1].set_exception(psycopg.OperationalError('failed'))
    assert 1 not in scheduler.in_flight
    scheduler._poll_once()
    assert scheduler.executor.jobs[-1][0] == 3


def test_database_failure_is_retried_and_shutdown_drains(scheduler):
    attempts = []
    def load():
        attempts.append(1)
        if len(attempts) == 1:
            raise psycopg.OperationalError('unavailable')
        scheduler.stop()
        return []
    scheduler._load_endpoints = load
    scheduler.poll_interval = .001
    scheduler.run()
    assert len(attempts) == 2
    assert scheduler.executor.closed
    assert not scheduler.running


def test_stop_prevents_dispatch(scheduler):
    scheduler.stop()
    scheduler._poll_once()
    assert not scheduler.executor.jobs


def test_readiness_heartbeat(scheduler, monkeypatch):
    monkeypatch.setattr('app.scheduler.time.monotonic', lambda: 100)
    assert not scheduler.healthy()
    scheduler.running = True
    scheduler.last_poll = 71
    assert scheduler.healthy()
    scheduler.last_poll = 70
    assert not scheduler.healthy()
    scheduler.last_poll = 100
    scheduler.stop()
    assert not scheduler.healthy()


@pytest.mark.parametrize('workers,interval', [(0,1),(-1,1),(1,0)])
def test_invalid_configuration(workers, interval):
    with pytest.raises(ValueError):
        HealthCheckScheduler(max_workers=workers, poll_interval=interval)
