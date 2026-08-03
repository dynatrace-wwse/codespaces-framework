"""Durable tenant-attribution registry (EPIC-002 §9, workstream D).

Records WHO deployed the Enablement App to WHICH tenant — the attribution
Dynatrace APIs cannot answer after the fact (OAuth-client owner email, account
name, envId→account mapping and token lookup-by-secret are all NOT retrievable;
see ops-server/docs/EPIC-002-ws-d-tenant-identity.md). So we capture identity
at the only moments we ever hold it:

  1. deploy time — every deploy call site in dashboard/app_deploy.py writes
     here best-effort (via = sso-deploy | auto | token | oauth-bootstrap);
     the OAuth-bootstrap path also records the accountUrn + clientId it was
     handed, and an optional deployer email from the Register Tenant form;
  2. runtime backstop — the app POSTs /api/tenants/register-identity on first
     admin visit (service bearer), merging the admin's email/name in.

Redis layout (Orbital Redis, same instance as jobs):
  tenant:registry:{tenant_id}  hash — accountUrn, clientId, deployerEmail,
                               friendlyName, via, firstSeen, lastDeploy,
                               appVersion, identityEmail, identityName, lastSeen
  tenant:registry:index        set of registered tenant_ids

The shaping/merge logic is pure (no Redis) and unit-tested in
test_tenant_registry.py; the async helpers below are thin wrappers.
tenant_map.json (content delivery) is intentionally untouched — different
concern, different lifecycle.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("ops-dashboard.tenant-registry")

INDEX_KEY = "tenant:registry:index"

# Deploy attribution channels (see app_deploy.py call sites).
DEPLOY_VIAS = ("sso-deploy", "auto", "token", "oauth-bootstrap")


def registry_key(tenant_id: str) -> str:
    return f"tenant:registry:{tenant_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Pure shaping / merge logic ───────────────────────────────────────────────

def shape_deploy(via: str, *, account_urn: str = "", client_id: str = "",
                 deployer: str = "", friendly_name: str = "",
                 app_version: str = "", now: str | None = None) -> dict:
    """Fields a deploy call site contributes. Empty values are DROPPED so a
    later, less-informed deploy (e.g. token after oauth-bootstrap) never
    blanks attribution captured earlier."""
    fields = {"via": via, "lastDeploy": now or _now_iso()}
    if account_urn:
        fields["accountUrn"] = account_urn
    if client_id:
        fields["clientId"] = client_id
    if deployer:
        fields["deployerEmail"] = deployer
    if friendly_name:
        fields["friendlyName"] = friendly_name
    if app_version:
        fields["appVersion"] = app_version
    return fields


def merge_fields(existing: dict, fields: dict) -> dict:
    """The hash fields to actually write: the incoming fields (already
    empty-stripped by shape_*) plus a firstSeen stamped exactly once."""
    out = dict(fields)
    if not (existing or {}).get("firstSeen"):
        out["firstSeen"] = (fields.get("lastDeploy") or fields.get("lastSeen")
                            or _now_iso())
    return out


def merge_identity(existing: dict, *, email: str = "", name: str = "",
                   account_urn: str = "", friendly_name: str = "",
                   now: str | None = None) -> dict:
    """Runtime-backstop merge (POST /api/tenants/register-identity): always
    refresh lastSeen + the identity fields; FILL deployerEmail only when the
    deploy-time record left it empty (deploy-time attribution wins).
    friendlyName (registrant-supplied — the account name is not retrievable
    via API) is set when provided, never blanked by an empty value."""
    now = now or _now_iso()
    out: dict = {"lastSeen": now}
    if email:
        out["identityEmail"] = email
        if not (existing or {}).get("deployerEmail"):
            out["deployerEmail"] = email
    if name:
        out["identityName"] = name
    if account_urn:
        out["accountUrn"] = account_urn
    if friendly_name:
        out["friendlyName"] = friendly_name
    return merge_fields(existing or {}, out)


# ── Async Redis helpers ──────────────────────────────────────────────────────

async def record_deploy(pool, tenant_id: str, via: str, *,
                        account_urn: str = "", client_id: str = "",
                        deployer: str = "", friendly_name: str = "",
                        app_version: str = "") -> None:
    """Best-effort registry write at a deploy call site — a registry failure
    must NEVER break a deploy, so every error is swallowed (logged)."""
    try:
        key = registry_key(tenant_id)
        existing = await pool.hgetall(key)
        fields = merge_fields(existing or {}, shape_deploy(
            via, account_urn=account_urn, client_id=client_id,
            deployer=deployer, friendly_name=friendly_name,
            app_version=app_version))
        await pool.hset(key, mapping=fields)
        await pool.sadd(INDEX_KEY, tenant_id)
    except Exception as exc:
        log.warning("tenant-registry write failed for %s (via=%s): %s",
                    tenant_id, via, exc)


async def record_identity(pool, tenant_id: str, *, email: str = "",
                          name: str = "", account_urn: str = "",
                          friendly_name: str = "") -> dict:
    """Merge the app-reported admin identity into the registry and return the
    resulting entry. Raises on Redis errors (the endpoint reports them)."""
    key = registry_key(tenant_id)
    existing = await pool.hgetall(key) or {}
    fields = merge_identity(existing, email=email, name=name,
                            account_urn=account_urn,
                            friendly_name=friendly_name)
    await pool.hset(key, mapping=fields)
    await pool.sadd(INDEX_KEY, tenant_id)
    return {**existing, **fields, "tenant": tenant_id}


async def list_entries(pool) -> list[dict]:
    """All registry entries, sorted by tenant id. Auth-gated by the caller —
    values are returned unmasked."""
    ids = sorted(await pool.smembers(INDEX_KEY) or [])
    out: list[dict] = []
    for tid in ids:
        h = await pool.hgetall(registry_key(tid))
        if h:
            out.append({"tenant": tid, **h})
    return out
