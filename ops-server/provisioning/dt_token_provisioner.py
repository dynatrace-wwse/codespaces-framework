"""Dynatrace API token provisioner for training sessions.

Creates scoped, time-limited API tokens on behalf of a training user.
Supports two auth modes:

  oauth2    — client_credentials flow. Used when the Arena app is installed on
              a tenant and provides its OAuth2 client ID + secret. The app must
              have the token:write scope declared in its manifest.

  api_token — an existing API token with apiTokens.write scope. Used for the
              QA validation tenant and for manual/bootstrap flows.

Token naming:  enablement-{repo_short}-{user_short}-{suffix}
Token expiry:  matches the training session TTL (default 4h)

Created token IDs are returned so they can be stored in Redis and revoked
when the session terminates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from .token_specs import TokenSpec

log = logging.getLogger("ops-provisioning")

_TOKEN_API = "{tenant}/api/v2/apiTokens"
_OAUTH_TOKEN_URL = "{tenant}/sso/oauth2/token"
_OAUTH_SCOPES = "token:write offline_access"


@dataclass
class ProvisionedTokens:
    env: dict[str, str]       # env_var → token_value  (e.g. DT_OPERATOR_TOKEN → "dt0c01...")
    token_ids: list[str]      # DT token IDs for revocation
    expires_at: str           # ISO-8601 UTC
    tenant_url: str


class DTTokenProvisioner:
    """Create and revoke DT API tokens for a training session.

    Instantiate with ONE of:
      - api_token  → existing token with apiTokens.write scope
      - oauth_client_id + oauth_client_secret  → OAuth2 app credentials
    """

    def __init__(
        self,
        tenant_url: str,
        api_token: str = "",
        oauth_client_id: str = "",
        oauth_client_secret: str = "",
    ):
        self.tenant_url = tenant_url.rstrip("/")
        self._api_token = api_token
        self._oauth_client_id = oauth_client_id
        self._oauth_client_secret = oauth_client_secret
        self._bearer: Optional[str] = None
        self._bearer_expiry: Optional[datetime] = None

        if not api_token and not (oauth_client_id and oauth_client_secret):
            raise ValueError("Provide either api_token or oauth_client_id + oauth_client_secret")

    async def _auth_headers(self) -> dict[str, str]:
        if self._api_token:
            return {"Authorization": f"Api-Token {self._api_token}",
                    "Content-Type": "application/json"}

        now = datetime.now(timezone.utc)
        if not self._bearer or (self._bearer_expiry and now >= self._bearer_expiry):
            await self._refresh_bearer()

        return {"Authorization": f"Bearer {self._bearer}",
                "Content-Type": "application/json"}

    async def _refresh_bearer(self):
        url = _OAUTH_TOKEN_URL.format(tenant=self.tenant_url)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, data={
                "grant_type": "client_credentials",
                "client_id": self._oauth_client_id,
                "client_secret": self._oauth_client_secret,
                "scope": _OAUTH_SCOPES,
            })
            r.raise_for_status()
            data = r.json()
            self._bearer = data["access_token"]
            # Conservative expiry: shave 60s off expires_in
            expires_in = int(data.get("expires_in", 3600))
            self._bearer_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
            log.debug("Refreshed OAuth2 bearer token (expires_in=%ds)", expires_in)

    async def create_tokens(
        self,
        repo: str,
        user_id: str,
        specs: list[TokenSpec],
        expires_in_hours: int = 4,
    ) -> ProvisionedTokens:
        """Create all tokens defined in specs and return them as env vars.

        Token name format: enablement-{repo_short}-{user_short}-{suffix}
        """
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
        expires_iso = expires_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Build a safe prefix — no slashes, max 30 chars total
        repo_short = repo.split("/")[-1][:20].replace("_", "-")
        user_short = user_id.split("@")[0][:10].replace("_", "-").replace(".", "-")
        prefix = f"enbl-{repo_short}-{user_short}"

        headers = await self._auth_headers()
        token_api = _TOKEN_API.format(tenant=self.tenant_url)

        env: dict[str, str] = {}
        token_ids: list[str] = []
        errors: list[str] = []

        async with httpx.AsyncClient(timeout=20) as client:
            for spec in specs:
                name = f"{prefix}-{spec.name_suffix}"[:100]
                payload = {
                    "name": name,
                    "expirationDate": expires_iso,
                    "scopes": spec.scopes,
                }
                try:
                    r = await client.post(token_api, headers=headers, json=payload)
                    r.raise_for_status()
                    data = r.json()
                    env[spec.env_var] = data["token"]
                    token_ids.append(data["id"])
                    log.info("Created token '%s' (id=%s, expiry=%s)", name, data["id"], expires_iso)
                except httpx.HTTPStatusError as exc:
                    msg = f"Failed to create token '{name}': HTTP {exc.response.status_code} — {exc.response.text[:200]}"
                    log.error(msg)
                    errors.append(msg)

        if errors:
            # Revoke any tokens already created before raising
            if token_ids:
                await self.revoke_tokens(token_ids)
            raise RuntimeError(f"Token provisioning failed:\n" + "\n".join(errors))

        # Also expose DT_ENVIRONMENT so executor can write a complete .env
        env["DT_ENVIRONMENT"] = self.tenant_url

        return ProvisionedTokens(
            env=env,
            token_ids=token_ids,
            expires_at=expires_iso,
            tenant_url=self.tenant_url,
        )

    async def revoke_tokens(self, token_ids: list[str]):
        """Revoke all provisioned tokens. Best-effort — logs but does not raise."""
        if not token_ids:
            return
        headers = await self._auth_headers()
        token_api = _TOKEN_API.format(tenant=self.tenant_url)

        async with httpx.AsyncClient(timeout=15) as client:
            for tid in token_ids:
                try:
                    r = await client.delete(f"{token_api}/{tid}", headers=headers)
                    if r.status_code in (200, 204, 404):
                        log.info("Revoked token %s (status=%d)", tid, r.status_code)
                    else:
                        log.warning("Unexpected status revoking token %s: %d", tid, r.status_code)
                except Exception as exc:
                    log.warning("Could not revoke token %s: %s", tid, exc)


# ── Account Management platform-token provisioner (gen3 / migrated tenants) ──────
#
# On tenants where classic apiToken creation is disabled (sprint, and prod as it
# migrates: POST /platform/classic/environment-api/v2/apiTokens → 400 "only available
# in Account Management"), training tokens must be minted as PLATFORM tokens (dt0s16)
# via the Account Management API, using an ACCOUNT-level OAuth client.
#
# Verified live on sprint (ydi9582h / account ceae4b9d…) 2026-06-19:
#   bearer:  POST {sso}/sso/oauth2/token  client_credentials
#            scope="platform-token:tokens:write platform-token:tokens:manage"
#            resource="urn:dtaccount:{uuid}"
#   create:  POST {accountApi}/iam/v1/accounts/{uuid}/platform-tokens
#            {name, scope:[...], resource:["urn:dtenvironment:{envId}"], tags:[...],
#             expirationDate}  → {tokenId, token}      (userUuid NOT required for the client)
#   revoke:  DELETE {accountApi}/iam/v1/accounts/{uuid}/platform-tokens/{tokenId}

_PT_SCOPE = "platform-token:tokens:write platform-token:tokens:manage"


class PlatformTokenProvisioner:
    """Mint/revoke training tokens as Account Management platform tokens (gen3 path).

    `env_id` is the tenant subdomain (e.g. 'ydi9582h'); the token is scoped to
    `urn:dtenvironment:{env_id}`. Mirrors DTTokenProvisioner's ProvisionedTokens output
    so the rest of the provisioning flow is unchanged.
    """

    def __init__(self, tenant_url: str, env_id: str, account_uuid: str,
                 sso_token_url: str, account_api_host: str,
                 oauth_client_id: str, oauth_client_secret: str):
        self.tenant_url = tenant_url.rstrip("/")
        self.env_id = env_id
        self.account_uuid = account_uuid
        self.sso_token_url = sso_token_url
        self.account_api_host = account_api_host.rstrip("/")
        self._cid = oauth_client_id
        self._csec = oauth_client_secret
        if not (env_id and account_uuid and sso_token_url and account_api_host and oauth_client_id and oauth_client_secret):
            raise ValueError("PlatformTokenProvisioner requires env_id, account_uuid, sso_token_url, account_api_host, oauth_client_id, oauth_client_secret")

    @property
    def _tokens_url(self) -> str:
        return f"{self.account_api_host}/iam/v1/accounts/{self.account_uuid}/platform-tokens"

    async def _bearer(self) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(self.sso_token_url, data={
                "grant_type": "client_credentials",
                "client_id": self._cid,
                "client_secret": self._csec,
                "scope": _PT_SCOPE,
                "resource": f"urn:dtaccount:{self.account_uuid}",
            })
            r.raise_for_status()
            return r.json()["access_token"]

    async def _env_bearer(self, scope: str) -> str:
        """Bearer scoped to the ENVIRONMENT (urn:dtenvironment) — for tenant-level APIs
        such as ActiveGate-token creation (vs the account-scoped _bearer for platform tokens)."""
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(self.sso_token_url, data={
                "grant_type": "client_credentials", "client_id": self._cid,
                "client_secret": self._csec, "scope": scope,
                "resource": f"urn:dtenvironment:{self.env_id}"})
            r.raise_for_status()
            return r.json()["access_token"]

    async def create_activegate_token(self, name: str, expires_in_hours: int = 4) -> dict:
        """Create an ENVIRONMENT ActiveGate token (dt0g02) on the tenant — for DynaKube's
        ActiveGate. The classic apiTokens endpoint is disabled on gen3, but the
        ActiveGate-token endpoint still works with an OAuth bearer holding
        `environment-api:activegate-tokens:write`. Returns {id, token}. Verified live on
        sprint 2026-06-19. This is what unblocks DynaKube provisioning on gen3 (the operator
        no longer needs to self-mint an AG token — Orbital pre-creates it)."""
        expires_iso = (datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        bearer = await self._env_bearer("environment-api:activegate-tokens:write")
        url = f"{self.tenant_url}/platform/classic/environment-api/v2/activeGateTokens"
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {bearer}",
                                                "Content-Type": "application/json"},
                                  json={"name": name[:100], "activeGateType": "ENVIRONMENT",
                                        "expirationDate": expires_iso})
            r.raise_for_status()
            d = r.json()
            log.info("Created ActiveGate token '%s' (id=%s)", name, d.get("id"))
            return {"id": d.get("id"), "token": d.get("token")}

    async def create_tokens(self, repo: str, user_id: str, specs: list[TokenSpec],
                            expires_in_hours: int = 4) -> ProvisionedTokens:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
        expires_iso = expires_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        repo_short = repo.split("/")[-1][:20].replace("_", "-")
        user_short = user_id.split("@")[0][:10].replace("_", "-").replace(".", "-")
        prefix = f"enbl-{repo_short}-{user_short}"
        resource = [f"urn:dtenvironment:{self.env_id}"]

        bearer = await self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
        env: dict[str, str] = {}
        token_ids: list[str] = []
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=20) as client:
            for spec in specs:
                name = f"{prefix}-{spec.name_suffix}"[:100]
                payload = {"name": name, "scope": spec.scopes, "resource": resource,
                           "tags": ["enablement", repo_short], "expirationDate": expires_iso}
                try:
                    r = await client.post(self._tokens_url, headers=headers, json=payload)
                    r.raise_for_status()
                    data = r.json()
                    env[spec.env_var] = data["token"]
                    token_ids.append(data.get("tokenId") or data.get("id"))
                    log.info("Created platform token '%s' (id=%s)", name, token_ids[-1])
                except httpx.HTTPStatusError as exc:
                    errors.append(f"platform token '{name}': HTTP {exc.response.status_code} — {exc.response.text[:200]}")
        if errors:
            if token_ids:
                await self.revoke_tokens(token_ids)
            raise RuntimeError("Platform-token provisioning failed:\n" + "\n".join(errors))
        env["DT_ENVIRONMENT"] = self.tenant_url
        return ProvisionedTokens(env=env, token_ids=token_ids, expires_at=expires_iso, tenant_url=self.tenant_url)

    async def revoke_tokens(self, token_ids: list[str]):
        if not token_ids:
            return
        bearer = await self._bearer()
        headers = {"Authorization": f"Bearer {bearer}"}
        async with httpx.AsyncClient(timeout=15) as client:
            for tid in token_ids:
                if not tid:
                    continue
                try:
                    r = await client.delete(f"{self._tokens_url}/{tid}", headers=headers)
                    if r.status_code in (200, 204, 404):
                        log.info("Revoked platform token %s (status=%d)", tid, r.status_code)
                    else:
                        log.warning("Unexpected status revoking platform token %s: %d", tid, r.status_code)
                except Exception as exc:
                    log.warning("Could not revoke platform token %s: %s", tid, exc)
