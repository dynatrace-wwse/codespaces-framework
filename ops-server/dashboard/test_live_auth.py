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


# ---------------------------------------------------------------------------
# BUG-MASK-1 regression: the app proxies EVERY learner-facing workshop read
# (progress board, session detail/summary) with the service bearer. The bearer
# authenticates the app->Orbital transport; it must NOT, on its own, unmask the
# whole cohort's emails+tenants to the learner it is acting for. _sees_full_identities
# encodes that rule; is_trainer (per endpoint) still grants the trainer full view.
# ---------------------------------------------------------------------------

def _req(headers):
    """Minimal Request stub: only .headers.get(lowercased) is used by the gate."""
    class _H(dict):
        def get(self, k, default=""):
            return dict.get(self, k.lower(), default)
    hdr = _H({k.lower(): v for k, v in headers.items()})
    return type("_R", (), {"headers": hdr})()


def test_sees_full_identities_service_bearer_with_learner_caller_is_masked():
    r = _req({"Authorization": "Bearer test-service-token"})
    assert a._sees_full_identities(r, "learner@example.com") is False


def test_sees_full_identities_service_bearer_no_caller_is_masked():
    # Reversed deliberately (2026-08-06). This used to be True: a bearer with no
    # learner acting-for was read as "internal automation" and got the full view.
    # The token is now compiled into the app bundle, so anyone who can load the
    # app can present it — and omitting the caller was the whole trick. A bearer
    # with no caller is now the LEAST trusted shape, not the most.
    r = _req({"Authorization": "Bearer test-service-token"})
    assert a._sees_full_identities(r, "") is False


def test_a_leaked_bearer_cannot_unmask_a_cohort_by_dropping_the_caller():
    # The concrete attack the reversal above closes.
    r = _req({"Authorization": "Bearer test-service-token"})
    assert a._sees_full_identities(r, "learner@example.com") is False
    assert a._sees_full_identities(r, "") is False


def test_sees_full_identities_signed_in_org_member_is_full():
    # nginx sets X-Auth-User only after oauth2-proxy validated the session
    r = _req({"X-Auth-User": "sergio"})
    assert a._sees_full_identities(r, "learner@example.com") is True


def test_sees_full_identities_anonymous_is_masked():
    assert a._sees_full_identities(_req({}), "") is False


def test_sees_full_identities_wrong_bearer_is_masked():
    r = _req({"Authorization": "Bearer wrong"})
    assert a._sees_full_identities(r, "") is False


# ---------------------------------------------------------------------------
# Same principle, applied to the Workshops & Delivery admin routes. Those read
# EVERY tenant's workshops and rosters unmasked, so they use _require_writer
# rather than _require_service_or_writer: the baked bearer that every app
# install carries must not be a way in. Full coverage: test_workshops_admin.py.

def test_service_bearer_cannot_read_workshops_admin():
    r = client.get("/api/workshops/admin/trainers", headers=BEARER)
    assert r.status_code == 401
