"""Where a tenant's SSO lives — one resolver, shared by every minting path.

A Dynatrace OAuth client-credentials grant is posted to the *SSO* host, which is
NOT the tenant host. Formatting the tenant URL into a token endpoint produces a
301 and a mint that fails in a way that reads like a credential problem:

    Redirect response '301 Moved Permanently' for url
    'https://sro97894.apps.dynatrace.com/sso/oauth2/token'

That defect lived in ``dt_token_provisioner`` from the start and was invisible
because the app normally mints with its own identity — only a scripted caller
(a load test, a measurement harness) ever reached the code. This module exists
so there is exactly one answer to "which SSO host", reachable from both
``dashboard.app_deploy`` and ``provisioning`` without either importing the other.

Resolution order, most authoritative first:

1. **Ask the tenant.** ``HEAD /platform/oauth2/authorization/dynatrace-sso``
   redirects to its own SSO origin. This is the only source that stays correct
   when a new realm appears.
2. **Known-domain map.** The probe needs a network round trip and the tenant to
   be up. When it fails, a labs tenant must NOT silently fall back to the
   production SSO — that mints against the wrong realm and fails with a
   confusing 400. The suffix map keeps sprint/dev honest offline.
3. **Production SSO**, for everything else.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from shared.log_safety import scrub_for_log

log = logging.getLogger(__name__)

DEFAULT_SSO = "https://sso.dynatrace.com"

# Longest suffix wins, so the more specific labs domains are matched before any
# broader one that might be added later.
SSO_BY_DOMAIN_SUFFIX = {
    ".sprint.apps.dynatracelabs.com": "https://sso-sprint.dynatracelabs.com",
    ".sprint.dynatracelabs.com": "https://sso-sprint.dynatracelabs.com",
    ".dev.apps.dynatracelabs.com": "https://sso-dev.dynatracelabs.com",
    ".dev.dynatracelabs.com": "https://sso-dev.dynatracelabs.com",
}

TOKEN_PATH = "/sso/oauth2/token"

DEFAULT_ACCOUNT_API = "https://api.dynatrace.com"

# Account Management API host per realm. NOT derivable from the tenant or SSO domain —
# the labs realms answer on an internal hostname that shares no stem with either, which
# is why this is a table and why callers may override it. Mirrors
# dashboard.app_deploy.ACCOUNT_API_BY_DOMAIN; kept here so `provisioning` stays free of
# any dashboard import (see the module docstring).
ACCOUNT_API_BY_DOMAIN_SUFFIX = {
    ".sprint.apps.dynatracelabs.com": "https://api-hardening.internal.dynatracelabs.com",
    ".sprint.dynatracelabs.com": "https://api-hardening.internal.dynatracelabs.com",
    ".dev.apps.dynatracelabs.com": "https://api-hardening.internal.dynatracelabs.com",
    ".dev.dynatracelabs.com": "https://api-hardening.internal.dynatracelabs.com",
}


def _host(tenant_url: str) -> str:
    u = urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}")
    return u.netloc


def sso_for_known_domain(tenant_url: str) -> str:
    """SSO origin from the tenant's domain alone — no network.

    Used as the fallback when the probe cannot run, and directly by tests.
    """
    host = _host(tenant_url).lower()
    for suffix in sorted(SSO_BY_DOMAIN_SUFFIX, key=len, reverse=True):
        if host.endswith(suffix):
            return SSO_BY_DOMAIN_SUFFIX[suffix]
    return DEFAULT_SSO


def account_api_for(tenant_url: str) -> str:
    """Account Management API origin for ``tenant_url`` — no network."""
    host = _host(tenant_url).lower()
    for suffix in sorted(ACCOUNT_API_BY_DOMAIN_SUFFIX, key=len, reverse=True):
        if host.endswith(suffix):
            return ACCOUNT_API_BY_DOMAIN_SUFFIX[suffix]
    return DEFAULT_ACCOUNT_API


def environment_id(tenant_url: str) -> str:
    """The environment id (``sro97894``) from any of its URL shapes."""
    return _host(tenant_url).split(".")[0].lower()


# Hosts this module will make a request to. The probe below fetches a URL built
# from a CALLER-SUPPLIED tenant, from inside the ops server, which is an SSRF
# primitive: without a restriction, "tenant_url" of http://169.254.169.254/ makes
# Orbital fetch its own instance credentials and log the outcome. Discovery is
# only ever meant to reach a Dynatrace tenant, so say so.
PROBEABLE_SUFFIXES = (".dynatrace.com", ".dynatracelabs.com")


def is_probeable(tenant_url: str) -> bool:
    """Whether discovery may make a network request to this tenant.

    https only — a downgrade to http would put the probe on the wire in clear —
    and the host must be a Dynatrace one. Anything else falls back to the
    domain-suffix map, which needs no network and cannot be pointed anywhere.
    """
    u = urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}")
    if u.scheme != "https":
        return False
    host = (u.hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in PROBEABLE_SUFFIXES)


async def discover_sso(tenant_url: str) -> str:
    """The tenant's SSO origin (no path). Never raises."""
    if not is_probeable(tenant_url):
        log.debug("not probing %s — not an https Dynatrace host; using the domain map",
                  scrub_for_log(_host(tenant_url)))
        return sso_for_known_domain(tenant_url)
    try:
        u = urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}")
        # Rebuilt from the validated hostname and a fixed scheme/path, so nothing
        # the caller wrote reaches the request except a host that passed above.
        probe = f"https://{u.hostname}/platform/oauth2/authorization/dynatrace-sso"
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as c:
            r = await c.head(probe)
            loc = r.headers.get("location")
            if 300 <= r.status_code < 400 and loc:
                p = urlparse(loc)
                if p.scheme and p.netloc:
                    return f"{p.scheme}://{p.netloc}"
    except Exception as exc:
        # HOST only. A tenant URL can carry credentials in userinfo or a query
        # string, and this line runs on a path where the caller supplied it.
        log.warning("SSO discovery failed for %s: %s",
                    scrub_for_log(_host(tenant_url)), scrub_for_log(exc))
    return sso_for_known_domain(tenant_url)


async def discover_token_url(tenant_url: str) -> str:
    """Full client-credentials token endpoint for ``tenant_url``."""
    return f"{await discover_sso(tenant_url)}{TOKEN_PATH}"
