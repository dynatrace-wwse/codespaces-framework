"""Auth gating for the live-session write endpoints (PII epic).

Verifies the _require_service_or_writer contract over HTTP with Starlette's
TestClient: anonymous callers get 401 on every live-session write; a caller
presenting the configured ORBITAL_TOKEN bearer passes the gate (asserted via
a 400 from body validation — reached only after auth — so no Redis is needed).

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_live_auth.py
"""

import dashboard.app as a
from fastapi.testclient import TestClient

client = TestClient(a.app, raise_server_exceptions=False)

BEARER = {"Authorization": "Bearer test-service-token"}


def setup_module(_module):
    """Accept a known bearer regardless of the (test-runner) environment —
    other test files may import dashboard.app before this one, so the env
    var route is not reliable under a full-suite run."""
    _module._saved_tokens = a.ORBITAL_TOKENS
    a.ORBITAL_TOKENS = ("test-service-token",)


def teardown_module(_module):
    a.ORBITAL_TOKENS = _module._saved_tokens

WRITE_POSTS = [
    "/api/live/sessions",
    "/api/live/sessions/sid-1/start",
    "/api/live/sessions/sid-1/end",
    "/api/live/sessions/sid-1/cancel",
    "/api/live/sessions/sid-1/open-registration",
    "/api/live/sessions/join-by-code",
    "/api/live/sessions/sid-1/provision-all",
    "/api/live/sessions/sid-1/pad-token",
]


def test_orbital_token_configured():
    assert "test-service-token" in a.ORBITAL_TOKENS


def test_anonymous_write_posts_all_401():
    for path in WRITE_POSTS:
        r = client.post(path, json={})
        assert r.status_code == 401, f"{path} -> {r.status_code} (expected 401)"


def test_wrong_bearer_is_401():
    r = client.post("/api/live/sessions", json={},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_service_bearer_passes_gate_create():
    # Empty body fails validation with 400 — reached only AFTER the auth gate,
    # and before any Redis access (validate_create raises first).
    r = client.post("/api/live/sessions", json={}, headers=BEARER)
    assert r.status_code == 400


def test_service_bearer_passes_gate_join_by_code():
    # Missing code → 400 from normalize_join_code, again pre-Redis.
    r = client.post("/api/live/sessions/join-by-code", json={}, headers=BEARER)
    assert r.status_code == 400


def test_spoofed_x_auth_user_is_ignored_when_nginx_cleared_it():
    # nginx clears X-Auth-User on anonymous paths; FastAPI sees the empty
    # header exactly as if it were absent → still 401.
    r = client.post("/api/live/sessions", json={},
                    headers={"X-Auth-User": ""})
    assert r.status_code == 401
