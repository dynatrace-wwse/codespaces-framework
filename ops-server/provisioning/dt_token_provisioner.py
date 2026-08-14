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

from .sso import discover_token_url
from .token_specs import TokenSpec

log = logging.getLogger("ops-provisioning")

# Classic environment API, on the live domain. Takes `Api-Token dt0c01…`.
_CLASSIC_TOKEN_API = "{tenant}/api/v2/apiTokens"
# The same API behind the platform proxy, on the apps domain. Takes a Bearer
# (a dt0s16 platform token, or an OAuth client bearer). See token_api.
_PROXY_TOKEN_API = "{tenant}/platform/classic/environment-api/v2/apiTokens"
# NOT "{tenant}/sso/oauth2/token" — the grant goes to the SSO host, which is a
# different origin from the tenant. Posting it to the tenant returns 301 and the
# session then dies at postCreateCommand with no DT_OPERATOR_TOKEN. See
# provisioning/sso.py and docs/known-issues/arena-oauth-mint-sso-url.md.
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
        oauth_resource: str = "",
        oauth_token_url: str = "",
    ):
        self.tenant_url = tenant_url.rstrip("/")
        # The classic environment API (/api/v2/apiTokens) lives on the classic
        # domain, not the apps domain — POSTing to *.apps.dynatrace.com returns
        # 404 "request should go to *.live.dynatrace.com". Map gen3 URLs down;
        # classic/live URLs pass through untouched.
        self.classic_url = (
            self.tenant_url
            .replace(".apps.dynatrace.com", ".live.dynatrace.com")
            .replace(".sprint.apps.dynatracelabs.com", ".sprint.dynatracelabs.com")
            .replace(".dev.apps.dynatracelabs.com", ".dev.dynatracelabs.com")
        )
        self._api_token = api_token
        self._oauth_client_id = oauth_client_id
        self._oauth_client_secret = oauth_client_secret
        self._oauth_resource = oauth_resource
        # Empty means "discover on first mint". Passing it explicitly is for
        # callers that already know (and for tests, which must not hit network).
        self._oauth_token_url = oauth_token_url
        self._bearer: Optional[str] = None
        self._bearer_expiry: Optional[datetime] = None

        if not api_token and not (oauth_client_id and oauth_client_secret):
            raise ValueError("Provide either api_token or oauth_client_id + oauth_client_secret")

    @property
    def is_classic_token(self) -> bool:
        """Whether the supplied credential is a classic ``dt0c01`` API token.

        Everything else — a gen3 ``dt0s16`` platform token, or a bearer minted
        from an OAuth client — is an OAuth-family credential and has to go
        through the platform proxy instead. See :attr:`token_api`.
        """
        return self._api_token.startswith("dt0c01")

    @property
    def token_api(self) -> str:
        """Where to POST to create a token, for THIS credential.

        There are two endpoints and they accept different credentials, which is
        not obvious and cost a measurement run:

        * ``{live}/api/v2/apiTokens`` — the classic environment API. Takes an
          ``Api-Token dt0c01…`` header. A platform token gets 401 here
          ("Token exchange failed").
        * ``{tenant}/platform/classic/environment-api/v2/apiTokens`` — the same
          API behind the platform proxy. Takes a ``Bearer`` — a ``dt0s16``
          platform token or an OAuth client bearer. This is the ONLY endpoint
          that works for the credentials Orbital actually holds.

        Sending the right token to the wrong one of these looks exactly like a
        permissions problem, so it is chosen from the credential rather than
        configured.
        """
        if self.is_classic_token:
            return _CLASSIC_TOKEN_API.format(tenant=self.classic_url)
        return _PROXY_TOKEN_API.format(tenant=self.tenant_url)

    async def _auth_headers(self) -> dict[str, str]:
        if self._api_token:
            scheme = "Api-Token" if self.is_classic_token else "Bearer"
            return {"Authorization": f"{scheme} {self._api_token}",
                    "Content-Type": "application/json"}

        now = datetime.now(timezone.utc)
        if not self._bearer or (self._bearer_expiry and now >= self._bearer_expiry):
            await self._refresh_bearer()

        return {"Authorization": f"Bearer {self._bearer}",
                "Content-Type": "application/json"}

    async def _refresh_bearer(self):
        url = self._oauth_token_url or await discover_token_url(self.tenant_url)
        # Cache it: the discovery is a network round trip and the answer cannot
        # change for the life of a provisioner, which is one session.
        self._oauth_token_url = url
        form = {
            "grant_type": "client_credentials",
            "client_id": self._oauth_client_id,
            "client_secret": self._oauth_client_secret,
            "scope": _OAUTH_SCOPES,
        }
        # Account-scoped clients need the account URN as `resource`; app-installed
        # clients do not have one and must not send an empty string.
        if self._oauth_resource:
            form["resource"] = self._oauth_resource
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, data=form)
            if r.status_code >= 400:
                # The body is the only thing that separates a wrong scope from a
                # wrong resource — both are 400. Without it the caller sees a
                # bare status and guesses.
                log.error("OAuth mint HTTP %s at %s: %s",
                          r.status_code, url, r.text[:300])
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
        token_api = self.token_api

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
        token_api = self.token_api

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
