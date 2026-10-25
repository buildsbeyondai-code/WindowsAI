"""Host and Origin checks that close the DNS-rebinding hole.

The service binds to loopback and has no login, so the only thing standing
between a web page and the mutating API is the per-process CSRF token. That
token is handed out by ``GET /api/fixes``, which the same-origin policy keeps
away from other sites -- unless a site rebinds its own hostname to 127.0.0.1.
The browser then treats ``http://evil.example:5056`` as same-origin with the
dashboard, reads the token and can POST anything.

Rejecting every request whose ``Host`` is not a name this service is meant to
be reached by defeats that: a rebound page still sends ``Host: evil.example``.
``Origin`` is checked on state-changing requests as a second, independent
layer (browsers always send it on cross-origin POSTs).
"""
from __future__ import annotations

from urllib.parse import urlsplit

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _split_host(value: str) -> tuple:
    """('name', port or None) from a Host header value; ('', None) if unusable."""
    value = (value or "").strip().lower()
    if not value:
        return "", None
    if value.startswith("["):                       # [::1]:5056
        name, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        name, _, port = value.partition(":")
    elif ":" in value:                              # bare IPv6 without brackets
        name, port = value, ""
    else:
        name, port = value, ""
    if port and not port.isdigit():
        return "", None
    return name.rstrip("."), (int(port) if port else None)


def allowed_names(bind_host: str, extra=()) -> frozenset:
    """Loopback names, the bind address when it is not a wildcard, and extras."""
    names = set(LOOPBACK_NAMES)
    if bind_host and bind_host not in ("0.0.0.0", "::"):
        names.add(bind_host.strip("[]").lower())
    names.update(str(x).strip().lower() for x in extra if str(x).strip())
    return frozenset(names)


def host_ok(host_header: str, names: frozenset, port: int) -> bool:
    """True when Host names this service (any listed name, our port or none)."""
    name, p = _split_host(host_header)
    return bool(name) and name in names and p in (None, port)


def origin_ok(origin: str | None, names: frozenset, port: int) -> bool:
    """True when there is no Origin, or it is http(s) on an allowed name/port."""
    if origin is None:
        return True                    # not a browser cross-site request
    if origin == "null":
        return False                   # sandboxed frame / file: page
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        p = parts.port
    except ValueError:
        return False
    return parts.hostname.lower() in names and p in (None, port)


def check(method: str, host_header: str, origin: str | None,
          names: frozenset, port: int) -> str | None:
    """Reason to refuse the request, or None when it may proceed."""
    if not host_ok(host_header, names, port):
        return "host not allowed"
    if method.upper() not in SAFE_METHODS and not origin_ok(origin, names, port):
        return "cross-origin request refused"
    return None
