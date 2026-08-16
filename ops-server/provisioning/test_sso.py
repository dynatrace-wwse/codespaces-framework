"""Tests for SSO/account-API resolution — and for what discovery may fetch.

Run: /home/ops/ops-venv/bin/python -m provisioning.test_sso
  or pytest provisioning/test_sso.py
"""
from .sso import (DEFAULT_ACCOUNT_API, DEFAULT_SSO, account_api_for,
                  environment_id, is_probeable, sso_for_known_domain)


def test_production_and_labs_realms_resolve_differently():
    assert sso_for_known_domain("https://sro97894.apps.dynatrace.com") == DEFAULT_SSO
    assert sso_for_known_domain("https://ydi9582h.sprint.apps.dynatracelabs.com") \
        == "https://sso-sprint.dynatracelabs.com"


def test_account_api_is_not_derivable_from_the_tenant_domain():
    # The labs realm answers on a host that shares no stem with the tenant or the
    # SSO — the reason this is a table and not a string transform.
    assert account_api_for("https://sro97894.apps.dynatrace.com") == DEFAULT_ACCOUNT_API
    assert account_api_for("https://ydi9582h.sprint.apps.dynatracelabs.com") \
        == "https://api-hardening.internal.dynatracelabs.com"


def test_environment_id_from_any_url_shape():
    for url in ("https://sro97894.apps.dynatrace.com",
                "https://sro97894.live.dynatrace.com/",
                "sro97894.apps.dynatrace.com"):
        assert environment_id(url) == "sro97894"


# ── what discovery is allowed to fetch ──────────────────────────────────────

def test_discovery_only_probes_https_dynatrace_hosts():
    """The probe builds a URL from a CALLER-SUPPLIED tenant and fetches it from
    inside the ops server. Unrestricted, that is an SSRF primitive: a tenant of
    http://169.254.169.254/ makes Orbital fetch its own instance credentials."""
    assert is_probeable("https://sro97894.apps.dynatrace.com")
    assert is_probeable("https://ydi9582h.sprint.apps.dynatracelabs.com")

    # The instance metadata endpoint, by either scheme.
    assert not is_probeable("http://169.254.169.254/")
    assert not is_probeable("https://169.254.169.254/")
    # A downgrade would put the probe on the wire in clear.
    assert not is_probeable("http://sro97894.apps.dynatrace.com")
    # Unrelated hosts.
    assert not is_probeable("https://evil.example.com/")
    # Suffix confusion: the string contains "dynatrace.com" and must still fail.
    assert not is_probeable("https://dynatrace.com.evil.example.com/")
    assert not is_probeable("https://notdynatrace.com/")


def test_the_probe_url_is_rebuilt_and_carries_nothing_the_caller_wrote():
    """The reason validation and request share one parse.

    A tenant URL can smuggle credentials in userinfo, and everything before the
    LAST '@' is userinfo — so this parses to the legitimate host. The probe is
    rebuilt from that hostname with a fixed scheme and path, so the userinfo and
    the decoy host never reach the wire at all. Validating in one function and
    re-parsing in another would have left two chances to disagree about which
    half is the host.
    """
    from .sso import probe_url_for
    url = probe_url_for("https://user:pw@evil.example.com\\@sro97894.apps.dynatrace.com/")
    assert url == ("https://sro97894.apps.dynatrace.com"
                   "/platform/oauth2/authorization/dynatrace-sso")
    assert "@" not in url and "evil.example.com" not in url and "pw" not in url


def test_a_refused_tenant_yields_no_url_at_all():
    from .sso import probe_url_for
    for bad in ("http://169.254.169.254/", "https://169.254.169.254/",
                "https://evil.example.com/", "http://sro97894.apps.dynatrace.com"):
        assert probe_url_for(bad) == "", bad


def test_an_unprobeable_tenant_still_resolves_offline():
    """Refusing to probe must not refuse to answer — the domain map is the whole
    point of having a fallback."""
    assert sso_for_known_domain("https://evil.example.com/") == DEFAULT_SSO


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
