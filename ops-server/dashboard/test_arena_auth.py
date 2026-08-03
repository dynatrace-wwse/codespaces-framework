"""Arena endpoint auth — compatibility-window contract (_require_arena_auth).

Three behaviours, verified over HTTP with Starlette's TestClient:
  1. COMPAT (ARENA_AUTH_ENFORCE unset): anonymous callers still pass the gate
     but an ARENA-LEGACY-CALLER warning is logged so they can be inventoried
     from the journal before enforcement flips.
  2. ENFORCE (ARENA_AUTH_ENFORCE=1): anonymous callers get 401 on every gated
     arena endpoint; the service bearer still passes.
  3. GET /api/arena/trainings stays public (catalog) in both modes.

Redis is replaced with an empty FakePool so gate-passing is observable as the
endpoint's own "not found"-style response (404 / expired / empty list) —
reached only AFTER the auth gate.

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_arena_auth.py
"""

import logging
import os

import dashboard.app as a
from fastapi.testclient import TestClient

client = TestClient(a.app, raise_server_exceptions=False)

BEARER = {"Authorization": "Bearer test-service-token"}


class FakePool:
    """Just enough async Redis for the arena endpoints, all empty."""

    async def get(self, key):
        # Non-empty catalog cache so /trainings and /provision never fall
        # through to the live GitHub scrape.
        return "[]" if key == a._ARENA_CATALOG_CACHE_KEY else None

    async def hgetall(self, key):
        return {}

    async def exists(self, key):
        return 0

    async def scan(self, cursor, match=None, count=None):
        return 0, []


def setup_module(_module):
    _module._saved_tokens = a.ORBITAL_TOKENS
    _module._saved_pool = a.pool
    a.ORBITAL_TOKENS = ("test-service-token",)
    a.pool = FakePool()
    os.environ.pop("ARENA_AUTH_ENFORCE", None)


def teardown_module(_module):
    a.ORBITAL_TOKENS = _module._saved_tokens
    a.pool = _module._saved_pool
    os.environ.pop("ARENA_AUTH_ENFORCE", None)


# (method, path, json-body or None). Bodies are minimally valid so pydantic
# validation (which runs BEFORE the handler/gate) does not mask the auth check.
GATED = [
    ("POST", "/api/arena/provision", {"trainingId": "x", "userId": "u"}),
    ("GET",  "/api/arena/sessions/job-1", None),
    ("GET",  "/api/arena/user-session?userId=u&trainingId=x", None),
    ("GET",  "/api/arena/active-sessions?userId=u", None),
    ("POST", "/api/arena/sessions/job-1/shell-token", None),
    ("POST", "/api/arena/sessions/job-1/exec", {"command": "true"}),
    ("POST", "/api/arena/sessions/job-1/exec-start", {"command": "true"}),
    ("GET",  "/api/arena/sessions/job-1/exec-status/e1", None),
    ("POST", "/api/arena/sessions/job-1/terminate", None),
]


def _call(method, path, body, headers=None):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=body, headers=headers)


# ── Compat window (default): anonymous allowed + logged loudly ───────────────

def test_compat_anonymous_passes_gate_and_logs_legacy(caplog):
    for method, path, body in GATED:
        with caplog.at_level(logging.WARNING, logger="ops-dashboard"):
            caplog.clear()
            r = _call(method, path, body)
            # Gate passed: the endpoint answered from (empty) state, not 401.
            assert r.status_code in (200, 404, 409), \
                f"{method} {path} -> {r.status_code} (expected pass-through)"
            assert any("ARENA-LEGACY-CALLER" in rec.getMessage()
                       for rec in caplog.records), \
                f"{method} {path}: no ARENA-LEGACY-CALLER warning logged"


def test_compat_bearer_passes_without_legacy_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="ops-dashboard"):
        r = client.post("/api/arena/sessions/job-1/shell-token", headers=BEARER)
    assert r.status_code == 404  # empty FakePool — session unknown, gate passed
    assert not any("ARENA-LEGACY-CALLER" in rec.getMessage()
                   for rec in caplog.records)


def test_compat_legacy_warning_names_method_path(caplog):
    with caplog.at_level(logging.WARNING, logger="ops-dashboard"):
        client.post("/api/arena/sessions/job-9/shell-token")
    msgs = [rec.getMessage() for rec in caplog.records
            if "ARENA-LEGACY-CALLER" in rec.getMessage()]
    assert msgs and "POST" in msgs[0] and "/api/arena/sessions/job-9/shell-token" in msgs[0]


# ── Enforcement (ARENA_AUTH_ENFORCE=1): anonymous → 401, bearer passes ───────

def test_enforce_anonymous_401_everywhere():
    os.environ["ARENA_AUTH_ENFORCE"] = "1"
    try:
        for method, path, body in GATED:
            r = _call(method, path, body)
            assert r.status_code == 401, \
                f"{method} {path} -> {r.status_code} (expected 401)"
    finally:
        os.environ.pop("ARENA_AUTH_ENFORCE", None)


def test_enforce_wrong_bearer_is_401():
    os.environ["ARENA_AUTH_ENFORCE"] = "1"
    try:
        r = client.post("/api/arena/sessions/job-1/shell-token",
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
    finally:
        os.environ.pop("ARENA_AUTH_ENFORCE", None)


def test_enforce_service_bearer_still_passes():
    os.environ["ARENA_AUTH_ENFORCE"] = "1"
    try:
        r = client.post("/api/arena/sessions/job-1/shell-token", headers=BEARER)
        assert r.status_code == 404  # past the gate, unknown session
    finally:
        os.environ.pop("ARENA_AUTH_ENFORCE", None)


def test_enforce_spoofed_empty_x_auth_user_is_401():
    # nginx clears X-Auth-User on /api/arena/ — an empty header must not
    # count as a signed-in member.
    os.environ["ARENA_AUTH_ENFORCE"] = "1"
    try:
        r = client.post("/api/arena/sessions/job-1/shell-token",
                        headers={"X-Auth-User": ""})
        assert r.status_code == 401
    finally:
        os.environ.pop("ARENA_AUTH_ENFORCE", None)


# ── Public catalog stays public ──────────────────────────────────────────────

def test_trainings_catalog_public_in_both_modes():
    assert client.get("/api/arena/trainings").status_code == 200
    os.environ["ARENA_AUTH_ENFORCE"] = "1"
    try:
        assert client.get("/api/arena/trainings").status_code == 200
    finally:
        os.environ.pop("ARENA_AUTH_ENFORCE", None)
