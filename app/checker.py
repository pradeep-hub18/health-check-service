"""Concurrent-worker-safe endpoint checks with atomic PostgreSQL persistence."""

import math
import os
import socket
import time

import requests
from requests.adapters import HTTPAdapter

from app.database import get_connection
from app.metrics import HEALTH_CHECKS_TOTAL, HEALTH_CHECK_DURATION_SECONDS
from app.url_policy import URLPolicyError, resolve_endpoint_address, validate_endpoint_url

def request_timeout():
    """Configured connect/read inactivity limit; not a total elapsed deadline."""
    try:
        seconds = float(os.getenv("CHECK_TIMEOUT_SECONDS", "5"))
    except ValueError:
        raise ValueError("CHECK_TIMEOUT_SECONDS must be finite and positive") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("CHECK_TIMEOUT_SECONDS must be finite and positive")
    return (seconds, seconds)


class PinnedAddressAdapter(HTTPAdapter):
    """Connect to a validated IP while verifying the original HTTPS identity."""

    def __init__(self, address, hostname):
        self.address = address
        self.hostname = hostname
        super().__init__(max_retries=0, pool_connections=1, pool_maxsize=1)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request, verify, cert
        )
        host_params["host"] = self.address
        if host_params["scheme"] == "https":
            pool_kwargs["server_hostname"] = self.hostname
            pool_kwargs["assert_hostname"] = self.hostname
        return host_params, pool_kwargs


def request_status_code(url):
    """Fetch headers only, without proxies, redirects, retries or body buffering."""
    timeout = request_timeout()
    parsed = validate_endpoint_url(url)
    address = resolve_endpoint_address(parsed)
    hostname = parsed.hostname.encode("idna").decode("ascii")
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None:
        host_header += f":{parsed.port}"

    with requests.Session() as session:
        session.trust_env = False  # No proxy environment or ambient .netrc credentials.
        adapter = PinnedAddressAdapter(address, hostname)
        session.mount(f"{parsed.scheme}://", adapter)
        prepared = session.prepare_request(
            requests.Request("GET", url, headers={"Host": host_header})
        )
        # Session.send(..., allow_redirects=False) still prepares a potential next
        # request and can consume a redirect body. Sending via the adapter avoids
        # that path; its urllib3 call uses redirect=False and preload_content=False.
        with adapter.send(
            prepared,
            timeout=timeout,
            stream=True,
            verify=True,
            proxies={},
        ) as response:
            return response.status_code


def check_endpoint(endpoint_id):
    """Check one enabled endpoint and atomically persist its result and transition."""
    with get_connection() as conn:
        endpoint = conn.execute(
            """
            SELECT id, url, expected_status_code
            FROM endpoints
            WHERE id = %s AND enabled = TRUE
            """,
            (endpoint_id,),
        ).fetchone()

    if endpoint is None:
        return {"error": "Enabled endpoint not found"}, 404

    start_time = time.perf_counter()
    actual_status = None
    error_message = None
    try:
        actual_status = request_status_code(endpoint["url"])
        success = actual_status == endpoint["expected_status_code"]
    except (requests.RequestException, socket.gaierror, URLPolicyError) as exc:
        success = False
        # Network exceptions commonly contain URLs and query-string credentials.
        # Persist the category only; never copy remote input into errors or logs.
        error_message = f"Request failed: {type(exc).__name__}"

    duration = time.perf_counter() - start_time
    HEALTH_CHECK_DURATION_SECONDS.observe(duration)
    HEALTH_CHECKS_TOTAL.labels(outcome="success" if success else "failure").inc()
    new_state = "healthy" if success else "unhealthy"

    with get_connection() as conn:
        with conn.transaction():
            previous = conn.execute(
                """
                SELECT current_state, enabled
                FROM endpoints
                WHERE id = %s
                FOR UPDATE
                """,
                (endpoint_id,),
            ).fetchone()
            if previous is None or not previous["enabled"]:
                return {"error": "Enabled endpoint no longer exists"}, 404

            old_state = previous["current_state"]
            conn.execute(
                """
                INSERT INTO check_results (
                    endpoint_id, success, actual_status_code,
                    response_time_ms, error_message
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (endpoint_id, success, actual_status, duration * 1000, error_message),
            )
            conn.execute(
                "UPDATE endpoints SET current_state = %s WHERE id = %s",
                (new_state, endpoint_id),
            )
            if old_state != new_state:
                conn.execute(
                    """
                    INSERT INTO state_transitions (endpoint_id, from_state, to_state)
                    VALUES (%s, %s, %s)
                    """,
                    (endpoint_id, old_state, new_state),
                )

    return {
        "endpoint_id": endpoint_id,
        "url": endpoint["url"],
        "success": success,
        "expected_status_code": endpoint["expected_status_code"],
        "actual_status_code": actual_status,
        "response_time_ms": round(duration * 1000, 2),
        "current_state": new_state,
        "error": error_message,
    }, 200
