"""Bounded, fair, single-process endpoint scheduling for the MVP."""
import logging
import os
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor

import psycopg

from app.checker import check_endpoint
from app.database import get_connection
from app.metrics import SCHEDULER_ERRORS, SCHEDULER_IN_FLIGHT, SCHEDULER_LAST_POLL

logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 1
MAX_WORKERS = 10


class HealthCheckScheduler:
    def __init__(self, max_workers=None, poll_interval=POLL_INTERVAL_SECONDS):
        self.max_workers = int(max_workers if max_workers is not None else
                               os.getenv("SCHEDULER_MAX_WORKERS", MAX_WORKERS))
        if self.max_workers < 1 or poll_interval <= 0:
            raise ValueError("Scheduler worker count and poll interval must be positive")
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.in_flight = set()
        self.lock = threading.Lock()
        self.last_check = {}
        self.last_poll = None
        self.running = False

    def healthy(self):
        return (self.running and not self.stop_event.is_set()
                and self.last_poll is not None
                and time.monotonic() - self.last_poll < 30)

    def _check_finished(self, endpoint_id, future):
        try:
            future.result()
        except CancelledError:
            pass
        except Exception:
            SCHEDULER_ERRORS.labels(stage="check").inc()
            # Avoid logging response payloads, URLs, or exception text with secrets.
            logger.error("Health check failed for endpoint %s", endpoint_id)
        finally:
            with self.lock:
                self.in_flight.discard(endpoint_id)
                SCHEDULER_IN_FLIGHT.set(len(self.in_flight))

    def _load_endpoints(self):
        with get_connection() as conn:
            return conn.execute(
                "SELECT id, interval_seconds FROM endpoints "
                "WHERE enabled = TRUE ORDER BY id"
            ).fetchall()

    def _poll_once(self):
        endpoints = self._load_endpoints()
        now = time.monotonic()
        self.last_poll = now
        SCHEDULER_LAST_POLL.set(time.time())
        active_ids = {endpoint["id"] for endpoint in endpoints}
        with self.lock:
            self.last_check = {eid: started for eid, started in self.last_check.items()
                               if eid in active_ids or eid in self.in_flight}
            # Oldest dispatch first: fast low-ID endpoints cannot starve new ones.
            endpoints.sort(key=lambda item: (
                self.last_check.get(item["id"], float("-inf")), item["id"]))

        for endpoint in endpoints:
            if self.stop_event.is_set():
                break
            endpoint_id = endpoint["id"]
            with self.lock:
                if len(self.in_flight) >= self.max_workers:
                    break
                due = (endpoint_id not in self.last_check or
                       now - self.last_check[endpoint_id] >= endpoint["interval_seconds"])
                if not due or endpoint_id in self.in_flight:
                    continue
                self.in_flight.add(endpoint_id)
                SCHEDULER_IN_FLIGHT.set(len(self.in_flight))
            try:
                future = self.executor.submit(check_endpoint, endpoint_id)
            except Exception:
                with self.lock:
                    self.in_flight.discard(endpoint_id)
                    SCHEDULER_IN_FLIGHT.set(len(self.in_flight))
                SCHEDULER_ERRORS.labels(stage="submit").inc()
                raise
            with self.lock:
                self.last_check[endpoint_id] = now
            # add_done_callback can invoke synchronously. Never hold self.lock here.
            future.add_done_callback(
                lambda completed, eid=endpoint_id: self._check_finished(eid, completed))

    def run(self):
        self.running = True
        logger.info("Health-check scheduler started with %s workers", self.max_workers)
        try:
            while not self.stop_event.is_set():
                try:
                    self._poll_once()
                except psycopg.Error:
                    SCHEDULER_ERRORS.labels(stage="load").inc()
                    logger.error("Unable to load endpoints from database")
                except Exception:
                    logger.exception("Unexpected scheduler error")
                self.stop_event.wait(self.poll_interval)
        finally:
            self.running = False
            self.stop_event.set()
            logger.info("Scheduler stopping; draining in-flight checks")
            self.executor.shutdown(wait=True, cancel_futures=True)
            logger.info("Health-check scheduler stopped")

    def stop(self):
        self.stop_event.set()
