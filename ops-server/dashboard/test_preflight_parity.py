"""One gate, two front doors — pinned.

The tenant checker and Register Tenant were independent implementations of the same
decision. Nothing imported one from the other and no test compared them, so they drifted
until an SE saw an all-green checker and an HTTP 412 register in the same minute
(`bnk46244`, 2026-08-24). These tests fail if that separation ever comes back.

Run: /home/ops/ops-venv/bin/python -m pytest dashboard/test_preflight_parity.py -q
"""

import pathlib
import re

import pytest

from dashboard import app_deploy as dep
from dashboard import tenant_preflight as pf
from dashboard.tenant_credentials import REGISTER_SCOPES, missing_from_catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHECKER = ROOT / "tools" / "tenant-check-page" / "check-tenant-setup.sh"
CHECKER_APP = ROOT / "tools" / "tenant-check-page" / "app.py"
NGINX = ROOT / "nginx" / "ops-server.conf"

ALL_SCOPES = {s for entry in REGISTER_SCOPES for s in entry}


# ── The checker must not re-implement the gate ───────────────────────────────

def test_the_checker_does_not_probe_the_tenant_itself():
    """It POSTs to /api/deploy/preflight. Any direct SSO or tenant-API call here is a
    second implementation, which is exactly what drifted."""
    body = CHECKER.read_text()
    assert "/api/deploy/preflight" in body
    for forbidden in ("sso.dynatrace.com/sso/oauth2/token",
                      "/platform/classic/environment-api",
                      "/platform/document/v1/documents",
                      "grant_type=client_credentials"):
        assert forbidden not in body, (
            f"{forbidden} is back in the checker — it is probing the tenant directly "
            f"again instead of asking the shared gate")


def test_the_checker_does_not_carry_its_own_scope_list():
    """A hardcoded copy is how the page ended up advertising only the classic ActiveGate
    scope while the gate accepted either that or the fleet-management twin."""
    body = CHECKER.read_text()
    listed = [s for s in ALL_SCOPES if s in body]
    assert not listed, f"the checker hardcodes scopes again: {listed}"


def test_the_page_renders_the_scope_panel_from_the_gate():
    body = CHECKER_APP.read_text()
    assert "/api/deploy/preflight-scopes" in body
    listed = [s for s in ALL_SCOPES if s in body]
    assert not listed, f"the page hardcodes the scope list again: {listed}"


def test_the_page_never_falls_back_to_a_baked_in_scope_list():
    """A stale list that looks authoritative is worse than no list."""
    body = CHECKER_APP.read_text()
    assert "stale list is worse than" in body


# ── The route the checker depends on must actually be reachable ──────────────

def test_both_shared_routes_exist():
    paths = {r.path for r in dep.router.routes if hasattr(r, "path")}
    assert "/api/deploy/preflight" in paths
    assert "/api/deploy/preflight-scopes" in paths


def test_nginx_lets_the_checker_reach_them_without_a_session():
    """The generic `^/api/deploy/` block requires an oauth2-proxy session. The checker
    calls server-to-server from GKE and has none, so both routes must sit in the
    anonymous-allowed alternation or they 401 in production and pass every unit test."""
    conf = NGINX.read_text()
    m = re.search(r"location ~ \^/api/deploy/\(([^)]*)\)\$", conf)
    assert m, "the anonymous-allowed deploy location block is gone"
    alternatives = m.group(1).split("|")
    assert "preflight" in alternatives
    assert "preflight-scopes" in alternatives


# ── One verdict, read by both doors ──────────────────────────────────────────

def test_ready_is_false_whenever_a_blocking_check_failed():
    report = pf.PreflightReport(
        client_exists=True, catalog=list(ALL_SCOPES), missing_scopes=[], checks=[
            pf.Check("a", "A", "pass", "", blocking=True),
            pf.Check("b", "B", "fail", "nope", blocking=True)])
    assert report.ready is False
    assert report.blocking_failures == ["B — nope"]


def test_a_non_blocking_failure_is_a_warning_not_a_refusal():
    report = pf.PreflightReport(
        client_exists=True, catalog=[], missing_scopes=[], checks=[
            pf.Check("a", "A", "pass", "", blocking=True),
            pf.Check("b", "B", "fail", "worth knowing", blocking=False)])
    assert report.ready is True
    assert report.warnings == ["B — worth knowing"]


def test_a_skipped_check_is_unproven_and_never_counted_as_a_pass():
    """The checker used to fold an unreachable probe into a green verdict."""
    report = pf.PreflightReport(
        client_exists=True, catalog=[], missing_scopes=[], checks=[
            pf.Check("a", "A", "skip", "could not run", blocking=True)])
    assert report.unproven == ["A — could not run"]
    assert report.blocking_failures == []          # a skip is not a failure...
    assert "A — could not run" in report.as_dict()["unproven"]   # ...but it IS reported


def test_a_client_that_does_not_exist_is_never_ready():
    report = pf.PreflightReport(client_exists=False, catalog=[], missing_scopes=[], checks=[])
    assert report.ready is False


def test_the_report_serialises_without_the_credential():
    report = pf.PreflightReport(
        client_exists=True, catalog=["a"], missing_scopes=[], checks=[
            pf.Check("k", "T", "pass", "d")])
    blob = str(report.as_dict())
    for secret in ("clientSecret", "csec", "dt0s02."):
        assert secret not in blob


# ── The app's self-update scope set still covers what Orbital blocks on ──────

def test_the_apps_deploy_scope_still_covers_every_blocking_capability():
    """`mintCredentials.function.ts` asserts this in a comment; nothing enforced it.
    An install-only bearer once made "Update now" a silent no-op on every tenant."""
    app_fn = ROOT.parent.parent / "dynatrace-app-enablements" / "api" / "mintCredentials.function.ts"
    if not app_fn.is_file():
        pytest.skip("the app repo is not checked out next to this one")
    m = re.search(r"export const DEPLOY_SCOPE\s*=\s*((?:\s*\"[^\"]*\")+)", app_fn.read_text())
    assert m, "DEPLOY_SCOPE is no longer a literal — this check cannot see it"
    granted = set(" ".join(re.findall(r'"([^"]*)"', m.group(1))).split())
    for cap in dep.BLOCKING_CAPABILITIES:
        needed = {s.strip() for s in dep.CAPABILITY_SCOPE[cap].split(",")}
        assert needed <= granted, (
            f"Orbital blocks a deploy on '{cap}' ({dep.CAPABILITY_SCOPE[cap]}) but the "
            f"app's DEPLOY_SCOPE does not ask for it — self-update will 412")


# ── The either-or ActiveGate entry ───────────────────────────────────────────

def test_a_client_with_only_the_gen3_activegate_scope_is_complete():
    catalog = {min(e) for e in REGISTER_SCOPES}
    catalog.discard("environment-api:activegate-tokens:write")
    catalog.add("fleet-management:activegate.tokens:write")
    assert missing_from_catalog(catalog) == []


def test_the_gate_and_the_scope_list_agree_on_the_activegate_pair():
    entry = next(e for e in REGISTER_SCOPES if "environment-api:activegate-tokens:write" in e)
    assert set(pf.AG_SCOPES) == set(entry), (
        "AG_SCOPES and REGISTER_SCOPES disagree on which scopes can mint a dt0g02")
