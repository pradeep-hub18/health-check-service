import socket

import pytest

from app.url_policy import (
    URLPolicyError,
    resolve_endpoint_address,
    validate_endpoint_url,
)


@pytest.fixture(autouse=True)
def public_targets_only(monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_ENDPOINTS", raising=False)


@pytest.mark.parametrize("url", [
    "https://example.com/health?check=1", "http://8.8.8.8:8080/",
    "https://[2606:4700:4700::1111]/", "https://bücher.example/",
])
def test_accepts_public_http_urls_without_resolving(url, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Registration must not perform DNS lookup")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert validate_endpoint_url(url).hostname


@pytest.mark.parametrize("url", [
    None, 42, "", "https://", "file:///etc/passwd", "ftp://example.com",
    "https://user:secret@example.com", "https://example.com:0",
    "https://example.com:65536", "https://example.com:", "https://[::1]suffix/", "https://example.com:bad", "https://[invalid]/",
    "https://example.com/#fragment", " https://example.com", "https://example.com\n",
    "https://example.com/white space", "https://example.com\x7f", "http://example.com\\@localhost",
    "https://-invalid.example/", "https://a..example/", "http://%31%32%37.0.0.1",
    "http://[fe80::1%25eth0]/", "https://example.com/" + "x" * 2048,
])
def test_rejects_malformed_urls_without_echoing_input(url):
    with pytest.raises(URLPolicyError) as error:
        validate_endpoint_url(url)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.0.0.1/", "http://172.16.0.1/",
    "http://192.168.1.1/", "http://169.254.169.254/", "http://0.0.0.0/",
    "http://100.64.0.1/", "http://224.0.0.1/", "http://192.0.2.1/",
    "http://[::1]/", "http://[::]/", "http://[fc00::1]/", "http://[fe80::1]/",
    "http://[::ffff:127.0.0.1]/", "http://[2002:7f00:1::]/",
    "http://[64:ff9b::7f00:1]/", "http://localhost", "http://a.localhost./",
])
def test_blocks_non_public_literal_destinations(url):
    with pytest.raises(URLPolicyError, match="public"):
        validate_endpoint_url(url)


def test_private_destinations_require_explicit_true(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_ENDPOINTS", "true")
    assert validate_endpoint_url("http://127.0.0.1/").hostname == "127.0.0.1"
    assert validate_endpoint_url("http://localhost/").hostname == "localhost"
    monkeypatch.setenv("ALLOW_PRIVATE_ENDPOINTS", "false")
    with pytest.raises(URLPolicyError):
        validate_endpoint_url("http://localhost/")


def answers(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


def test_resolves_once_and_returns_validated_ip(monkeypatch):
    calls = []
    def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return answers("8.8.8.8", "1.1.1.1")
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert resolve_endpoint_address(validate_endpoint_url("https://example.com")) == "8.8.8.8"
    assert calls == [(("example.com", 443), {"type": socket.SOCK_STREAM})]


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("8.8.8.8", "10.0.0.1"), ()])
def test_blocks_private_or_empty_dns_results(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers(*addresses))
    with pytest.raises(URLPolicyError):
        resolve_endpoint_address(validate_endpoint_url("http://example.com/"))


def test_trusted_internal_dns_is_allowed_when_enabled(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_ENDPOINTS", "true")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers("10.0.0.1"))
    assert resolve_endpoint_address(validate_endpoint_url("http://service.internal")) == "10.0.0.1"
