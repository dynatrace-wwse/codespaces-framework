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
    # Derived from whatever MINT_RESOURCE_SPRINT is in scope: the fallback above only
    # applies when the real /home/ops/.env has not already populated it (setdefault),
    # so hardcoding the fake uuid fails whenever another test module loads the env first.
    assert p.account_uuid == os.environ["MINT_RESOURCE_SPRINT"].rsplit(":", 1)[-1]
    assert p.account_api_host == os.environ["MINT_API_HOST_SPRINT"]


def test_factory_none_when_no_creds_for_domain():
    # prod has no MINT_*_PROD creds → None (gen2 uses app self-mint / classic)
    assert a._gen3_platform_provisioner("https://geu80787.apps.dynatrace.com") is None


def test_factory_none_for_non_dynatrace():
    assert a._gen3_platform_provisioner("https://evil.example.com") is None


def test_oauth_bootstrap_stores_nothing():
    """The bootstrap deploy endpoint must NOT persist the OAuth client anywhere —
    Orbital holds no tenant credential at rest (self-managed tenants mint in-app)."""
    import inspect
    from dashboard import app_deploy
    src = inspect.getsource(app_deploy.deploy_with_oauth)
    assert "_encrypt" not in src, "bootstrap deploy must not encrypt+store the client"
    assert "setex" not in src and ".set(" not in src, "bootstrap deploy must not write the client to Redis"
    assert not hasattr(app_deploy, "get_registered_mint_client"), "registered-client store must be gone"
    assert not hasattr(app_deploy, "MINTCLIENT_KEY"), "mint-client Redis key must be gone"


def test_provision_route_registered():
    assert any(getattr(r, "path", "") == "/api/arena/provision" for r in a.app.routes)
    import inspect
    assert inspect.iscoroutinefunction(a.api_arena_provision)
