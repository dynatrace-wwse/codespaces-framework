"""gen3 mint wiring: _gen3_platform_provisioner builds a PlatformTokenProvisioner from
per-account MINT_* env, only for tenants whose account has creds configured."""
import os
for k, v in {"MINT_CLIENT_ID_SPRINT": "dt0s02.X", "MINT_CLIENT_SECRET_SPRINT": "s",
             "MINT_RESOURCE_SPRINT": "urn:dtaccount:abc-123",
             "MINT_SSO_SPRINT": "https://sso-sprint.dynatracelabs.com/sso/oauth2/token",
             "MINT_API_HOST_SPRINT": "https://api-hardening.internal.dynatracelabs.com"}.items():
    os.environ.setdefault(k, v)
import dashboard.app as a
from provisioning import PlatformTokenProvisioner


def test_factory_builds_for_sprint():
    p = a._gen3_platform_provisioner("https://ydi9582h.sprint.apps.dynatracelabs.com")
    assert isinstance(p, PlatformTokenProvisioner)
    assert p.env_id == "ydi9582h"
    assert p.account_uuid == "abc-123"
    assert p.account_api_host == "https://api-hardening.internal.dynatracelabs.com"


def test_factory_none_when_no_creds_for_domain():
    # prod has no MINT_*_PROD creds → None (gen2 uses app self-mint / classic)
    assert a._gen3_platform_provisioner("https://geu80787.apps.dynatrace.com") is None


def test_factory_none_for_non_dynatrace():
    assert a._gen3_platform_provisioner("https://evil.example.com") is None


def test_provision_route_registered():
    assert any(getattr(r, "path", "") == "/api/arena/provision" for r in a.app.routes)
    import inspect
    assert inspect.iscoroutinefunction(a.api_arena_provision)
