import io
import socket
from contextlib import contextmanager
from unittest.mock import MagicMock

import psycopg
import pytest
import requests
from urllib3.response import HTTPResponse

from app import checker
from app.url_policy import URLPolicyError


class Connection:
    def __init__(self, row, fail_on=None):
        self.row = row
        self.calls = []
        self.closed = False
        self.committed = False
        self.rolled_back = False
        self.fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.closed = True

    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True

    def execute(self, query, params):
        self.calls.append((" ".join(query.split()), params))
        if self.fail_on and self.fail_on in query:
            raise psycopg.OperationalError("database unavailable")
        cursor = MagicMock()
        cursor.fetchone.return_value = self.row
        return cursor


@pytest.fixture
def endpoint():
    return {"id": 7, "url": "https://example.com/health?token=sensitive", "expected_status_code": 200}


def wire_connections(monkeypatch, endpoint, previous=None, fail_on=None):
    first = Connection(endpoint)
    second = Connection(previous if previous is not None else {"current_state": "unknown", "enabled": True}, fail_on)
    connections = iter([first, second])
    monkeypatch.setattr(checker, "get_connection", lambda: next(connections))
    return first, second


def test_success_persists_result_state_and_initial_transition(monkeypatch, endpoint):
    first, second = wire_connections(monkeypatch, endpoint)
    monkeypatch.setattr(checker, "request_status_code", lambda url: 200)
    ticks = iter([100.0, 100.125])
    monkeypatch.setattr(checker.time, "perf_counter", lambda: next(ticks))
    result, status = checker.check_endpoint(7)
    assert status == 200
    assert result["success"] is True
    assert result["response_time_ms"] == 125
    assert result["current_state"] == "healthy"
    assert second.calls[1][1] == (7, True, 200, 125.0, None)
    assert second.calls[2][1] == ("healthy", 7)
    assert second.calls[3][1] == (7, "unknown", "healthy")
    assert second.committed and first.closed and second.closed


@pytest.mark.parametrize("old_state,status,expected_transition", [
    ("healthy", 200, False), ("unhealthy", 503, False),
    ("healthy", 500, True), ("unhealthy", 200, True),
])
def test_records_only_actual_state_transitions(monkeypatch, endpoint, old_state, status, expected_transition):
    _, conn = wire_connections(monkeypatch, endpoint, {"current_state": old_state, "enabled": True})
    monkeypatch.setattr(checker, "request_status_code", lambda url: status)
    result, code = checker.check_endpoint(7)
    transitions = [params for sql, params in conn.calls if "INSERT INTO state_transitions" in sql]
    assert bool(transitions) is expected_transition
    assert result["success"] is (status == 200)
    assert code == 200


@pytest.mark.parametrize("error", [
    requests.Timeout("https://example.com?token=sensitive"),
    requests.ConnectionError("secret query"),
    requests.exceptions.SSLError("secret query"),
    socket.gaierror("secret query"),
    URLPolicyError("secret query"),
])
def test_request_failures_are_recorded_without_leaking_urls(monkeypatch, endpoint, error):
    _, conn = wire_connections(monkeypatch, endpoint)
    def fail(url):
        raise error
    monkeypatch.setattr(checker, "request_status_code", fail)
    result, status = checker.check_endpoint(7)
    assert status == 200
    assert result["success"] is False
    assert result["actual_status_code"] is None
    assert result["error"] == f"Request failed: {type(error).__name__}"
    assert "sensitive" not in conn.calls[1][1][4]
    assert conn.calls[2][1] == ("unhealthy", 7)


def test_missing_enabled_endpoint_is_not_requested(monkeypatch):
    conn = Connection(None)
    monkeypatch.setattr(checker, "get_connection", lambda: conn)
    request = MagicMock()
    monkeypatch.setattr(checker, "request_status_code", request)
    assert checker.check_endpoint(7)[1] == 404
    request.assert_not_called()
    assert conn.closed


@pytest.mark.parametrize("previous", [None, {"current_state": "unknown", "enabled": False}])
def test_deleted_or_disabled_during_request_has_no_result_write(monkeypatch, endpoint, previous):
    first, second = wire_connections(monkeypatch, endpoint)
    second.row = previous
    monkeypatch.setattr(checker, "request_status_code", lambda url: 200)
    assert checker.check_endpoint(7)[1] == 404
    assert len(second.calls) == 1
    assert first.closed and second.closed


def test_database_connect_failure_propagates(monkeypatch):
    def fail():
        raise psycopg.OperationalError("unavailable")
    monkeypatch.setattr(checker, "get_connection", fail)
    with pytest.raises(psycopg.OperationalError):
        checker.check_endpoint(7)


@pytest.mark.parametrize("statement", ["INSERT INTO check_results", "UPDATE endpoints", "INSERT INTO state_transitions"])
def test_write_errors_roll_back_and_propagate(monkeypatch, endpoint, statement):
    _, conn = wire_connections(monkeypatch, endpoint, fail_on=statement)
    monkeypatch.setattr(checker, "request_status_code", lambda url: 200)
    with pytest.raises(psycopg.OperationalError):
        checker.check_endpoint(7)
    assert conn.rolled_back and not conn.committed and conn.closed


def test_https_pool_pins_ip_preserving_sni_and_certificate_verification():
    adapter = checker.PinnedAddressAdapter("8.8.8.8", "example.com")
    try:
        request = requests.Request("GET", "https://example.com:8443/health").prepare()
        pool = adapter.get_connection_with_tls_context(request, verify=True, proxies={})
        assert pool.host == "8.8.8.8"
        assert pool.port == 8443
        assert pool.conn_kw["server_hostname"] == "example.com"
        assert pool.assert_hostname == "example.com"
        assert pool.cert_reqs == "CERT_REQUIRED"
        assert adapter.max_retries.total == 0
    finally:
        adapter.close()


def test_redirect_is_not_followed_or_buffered_and_resources_close(monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_ENDPOINTS", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    resolver = MagicMock(return_value="8.8.8.8")
    monkeypatch.setattr(checker, "resolve_endpoint_address", resolver)
    raw = HTTPResponse(body=io.BytesIO(b"unread"), status=302,
                       headers={"Location": "http://127.0.0.1/secret"}, preload_content=False)
    raw.read = MagicMock(side_effect=AssertionError("Response body must not be read"))
    pool = MagicMock()
    pool.urlopen.return_value = raw
    captured = []
    def get_pool(self, request, verify, proxies=None, cert=None):
        captured.append((request, verify, proxies))
        return pool
    monkeypatch.setattr(checker.PinnedAddressAdapter, "get_connection_with_tls_context", get_pool)
    assert checker.request_status_code("http://example.com:8080/health") == 302
    assert len(captured) == 1
    request, verify, proxies = captured[0]
    assert request.headers["Host"] == "example.com:8080"
    assert request.url == "http://example.com:8080/health"
    assert "Authorization" not in request.headers
    assert verify is True and proxies == {}
    kwargs = pool.urlopen.call_args.kwargs
    assert kwargs["redirect"] is False
    assert kwargs["preload_content"] is False
    assert kwargs["timeout"].connect_timeout == 5
    assert kwargs["timeout"].read_timeout == 5
    raw.read.assert_not_called()
    assert raw.closed
    resolver.assert_called_once()


def test_database_read_failure_closes_connection(monkeypatch, endpoint):
    first, _ = wire_connections(monkeypatch, endpoint)
    first.fail_on = "SELECT"
    with pytest.raises(psycopg.OperationalError):
        checker.check_endpoint(7)
    assert first.closed


def test_database_connection_failure_after_request_propagates(monkeypatch, endpoint):
    first = Connection(endpoint)
    calls = 0
    def connect():
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        raise psycopg.OperationalError("unavailable")
    monkeypatch.setattr(checker, "get_connection", connect)
    monkeypatch.setattr(checker, "request_status_code", lambda url: 200)
    with pytest.raises(psycopg.OperationalError):
        checker.check_endpoint(7)
    assert first.closed


@pytest.mark.parametrize("value", ["0", "-1", "nan", "NaN", "inf", "-inf", "invalid", ""])
def test_timeout_configuration_rejects_non_finite_or_non_positive_values(monkeypatch, value):
    monkeypatch.setenv("CHECK_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="finite and positive"):
        checker.request_timeout()


def test_timeout_configuration_defaults_to_five_seconds_and_accepts_fractional_values(monkeypatch):
    monkeypatch.delenv("CHECK_TIMEOUT_SECONDS", raising=False)
    assert checker.request_timeout() == (5.0, 5.0)
    monkeypatch.setenv("CHECK_TIMEOUT_SECONDS", "0.25")
    assert checker.request_timeout() == (0.25, 0.25)


def test_invalid_timeout_fails_before_dns_or_network(monkeypatch):
    monkeypatch.setenv("CHECK_TIMEOUT_SECONDS", "nan")
    resolver = MagicMock()
    monkeypatch.setattr(checker, "resolve_endpoint_address", resolver)
    with pytest.raises(ValueError, match="finite and positive"):
        checker.request_status_code("https://example.com/")
    resolver.assert_not_called()


@pytest.mark.integration
def test_local_https_preserves_sni_and_rejects_wrong_identity_or_untrusted_ca(monkeypatch, tmp_path):
    """Exercise the real TLS transport against localhost; no Internet or DB needed."""
    import shutil
    import ssl
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("Local TLS verification requires the openssl CLI")
    key = tmp_path / "test-only-private-key.pem"
    certificate = tmp_path / "test-only-certificate.pem"
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=target.example",
        "-addext", "subjectAltName=DNS:target.example",
    ], check=True, capture_output=True)
    observed = {"sni": [], "host": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed["host"].append(self.headers["Host"])
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(key))
    context.set_servername_callback(lambda sock, name, ctx: observed["sni"].append(name))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    default_ca_bundle = requests.adapters.DEFAULT_CA_BUNDLE_PATH
    monkeypatch.setattr(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", str(certificate))
    # Simulate the validated DNS answer; the request must connect to this IP,
    # although the URL, Host header and TLS identity all name target.example.
    monkeypatch.setattr(checker, "resolve_endpoint_address", lambda parsed: "127.0.0.1")
    try:
        assert checker.request_status_code(f"https://target.example:{server.server_port}/") == 204
        assert observed["sni"] == ["target.example"]
        assert observed["host"] == [f"target.example:{server.server_port}"]
        with pytest.raises(requests.exceptions.SSLError):
            checker.request_status_code(f"https://wrong.example:{server.server_port}/")
        monkeypatch.setattr(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", default_ca_bundle)
        with pytest.raises(requests.exceptions.SSLError):
            checker.request_status_code(f"https://target.example:{server.server_port}/")
        # Neither failed TLS handshake is allowed to send an HTTP request.
        assert len(observed["host"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
