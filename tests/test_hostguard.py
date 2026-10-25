"""Host/Origin guard (hostguard.py) and its wiring into every request.

The scenario it exists for: a page on evil.example rebinds its hostname to
127.0.0.1, reads the CSRF token from GET /api/fixes and POSTs a mutation. The
browser still sends ``Host: evil.example:5056``, so the request must be refused
before any route runs.
"""
import pytest
from tests.test_app import app

from sysperf import hostguard

NAMES = hostguard.allowed_names("127.0.0.1")


@pytest.fixture
def client():
    return app.app.test_client()


# ------------------------------------------------------------------- unit ---
@pytest.mark.parametrize("value, expected", [
    ("127.0.0.1:5056", ("127.0.0.1", 5056)),
    ("LocalHost", ("localhost", None)),
    ("localhost.:5056", ("localhost", 5056)),
    ("[::1]:5056", ("::1", 5056)),
    ("[::1]", ("::1", None)),
    ("::1", ("::1", None)),
    ("", ("", None)),
    ("127.0.0.1:abc", ("", None)),
])
def test_split_host(value, expected):
    assert hostguard._split_host(value) == expected


def test_allowed_names_adds_bind_address_and_extras_but_not_wildcards():
    assert hostguard.allowed_names("0.0.0.0") == frozenset(hostguard.LOOPBACK_NAMES)
    assert hostguard.allowed_names("::") == frozenset(hostguard.LOOPBACK_NAMES)
    names = hostguard.allowed_names("192.168.1.5", ["Dash.Lan", " ", ""])
    assert {"192.168.1.5", "dash.lan", "localhost"} <= names
    assert "" not in names
    assert hostguard.allowed_names("") == frozenset(hostguard.LOOPBACK_NAMES)


@pytest.mark.parametrize("host, ok", [
    ("127.0.0.1:5056", True), ("localhost", True), ("[::1]:5056", True),
    ("localhost:8080", False),           # our name, someone else's port
    ("evil.example:5056", False),        # the rebinding case
    ("evil.example", False), ("", False), ("127.0.0.1:x", False),
])
def test_host_ok(host, ok):
    assert hostguard.host_ok(host, NAMES, 5056) is ok


@pytest.mark.parametrize("origin, ok", [
    (None, True),                                  # curl, the desktop shell
    ("http://127.0.0.1:5056", True), ("http://localhost", True),
    ("https://localhost:5056", True),
    ("http://evil.example:5056", False), ("http://127.0.0.1:9999", False),
    ("null", False), ("file://", False), ("chrome-extension://abc", False),
    ("http://127.0.0.1:bad", False),
])
def test_origin_ok(origin, ok):
    assert hostguard.origin_ok(origin, NAMES, 5056) is ok


def test_check_order_and_safe_methods():
    assert hostguard.check("GET", "evil.example", None, NAMES, 5056) == "host not allowed"
    # A foreign Origin on a read is harmless (CORS keeps the response away).
    assert hostguard.check("GET", "localhost", "http://evil.example", NAMES, 5056) is None
    assert hostguard.check("post", "localhost", "http://evil.example", NAMES, 5056) == \
        "cross-origin request refused"
    assert hostguard.check("POST", "localhost:5056", "http://localhost:5056", NAMES,
                           5056) is None


# ----------------------------------------------------------------- wiring ---
def test_rebound_host_cannot_read_the_csrf_token(client):
    r = client.get("/api/fixes", headers={"Host": "evil.example:5056"})
    assert r.status_code == 403
    assert "token" not in r.get_json()
    assert r.get_json()["error"] == "host not allowed"


def test_rebound_host_cannot_post(client):
    r = client.post("/api/fix", json={"token": app.FIX_TOKEN, "confirm": True},
                    headers={"Host": "evil.example:5056"})
    assert r.status_code == 403


def test_cross_origin_post_refused_even_with_the_token(client):
    r = client.post("/api/autoimprove/mode", json={"token": app.FIX_TOKEN, "mode": "observe"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "cross-origin request refused"


def test_same_origin_requests_still_work(client):
    assert client.get("/api/health").status_code == 200
    r = client.get("/api/health", headers={"Host": "127.0.0.1:5056"})
    assert r.status_code == 200
    # Same-origin POST from the page itself (browser adds Origin): passes the
    # guard and reaches the route's own CSRF check.
    r = client.post("/api/fix", json={"confirm": True},
                    headers={"Host": "127.0.0.1:5056", "Origin": "http://127.0.0.1:5056"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "invalid or missing CSRF token"


def test_every_response_carries_a_content_security_policy(client):
    r = client.get("/api/health")
    csp = r.headers["Content-Security-Policy"]
    for part in ("default-src 'self'", "connect-src 'self'", "object-src 'none'",
                 "frame-ancestors 'self'"):
        assert part in csp
    refused = client.get("/api/health", headers={"Host": "evil.example"})
    assert refused.headers["Content-Security-Policy"] == csp
