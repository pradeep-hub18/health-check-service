"""HTTP target policy and DNS pinning for untrusted endpoint registration."""

import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit


class URLPolicyError(ValueError):
    """A safe-to-display target validation error, containing no input URL."""


def private_endpoints_allowed():
    return os.getenv("ALLOW_PRIVATE_ENDPOINTS", "false").lower() == "true"


def _public_address(address):
    ip = ipaddress.ip_address(address)
    # Reject transition mechanisms as well as local, reserved and multicast IPs.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _public_address(str(ip.ipv4_mapped))
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return False
        if ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def validate_endpoint_url(url):
    """Return SplitResult or raise ValueError; registration performs no DNS I/O."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise URLPolicyError("url must be an HTTP(S) URL of at most 2048 characters")
    try:
        url.encode("utf-8")
    except UnicodeError:
        raise URLPolicyError("url must contain valid Unicode characters") from None
    if any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url:
        raise URLPolicyError("url must not contain whitespace, control characters or backslashes")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise URLPolicyError("url must contain a valid hostname and port") from None
    if parsed.scheme not in ("http", "https") or not hostname:
        raise URLPolicyError("url must contain an http:// or https:// hostname")
    if parsed.username is not None or parsed.password is not None:
        raise URLPolicyError("url must not contain credentials")
    if parsed.fragment:
        raise URLPolicyError("url must not contain a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise URLPolicyError("url port must be between 1 and 65535")
    if parsed.netloc.startswith("["):
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            raise URLPolicyError("url brackets must contain a valid IPv6 address") from None
        if not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc):
            raise URLPolicyError("url must contain a valid hostname and port")
    elif parsed.netloc.endswith(":"):
        raise URLPolicyError("url must contain a valid port")
    if "%" in hostname:
        raise URLPolicyError("url must not contain an encoded hostname or IPv6 zone identifier")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii").rstrip(".")
        except UnicodeError:
            raise URLPolicyError("url must contain a valid hostname") from None
        labels = ascii_hostname.split(".")
        if len(ascii_hostname) > 253 or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        ):
            raise URLPolicyError("url must contain a valid hostname")
        if not private_endpoints_allowed() and (
            ascii_hostname.lower() == "localhost"
            or ascii_hostname.lower().endswith(".localhost")
        ):
            raise URLPolicyError("url must target a public address")
    else:
        if not private_endpoints_allowed() and not _public_address(hostname):
            raise URLPolicyError("url must target a public address")
    return parsed


def resolve_endpoint_address(parsed):
    """Resolve once, validate every answer, then return one IP to pin the socket."""
    hostname = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses:
        raise URLPolicyError("Target hostname did not resolve")
    if not private_endpoints_allowed() and any(
        not _public_address(address) for address in addresses
    ):
        raise URLPolicyError("Target hostname must resolve only to public addresses")
    return addresses[0]
