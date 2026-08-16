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

from .sso import account_api_for, discover_token_url, environment_id
from .token_specs import TokenSpec
from shared.log_safety import scrub_for_log

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
# Minting a gen3 dt0s16 is a different API with a different grant: the Account
# Management API, authorised by an account-scoped bearer carrying these scopes.
# A `kind: platform` spec can therefore only be served when an OAuth client and an
# account URN are available — a bare api_token cannot reach it.
_PLATFORM_TOKEN_SCOPES = "platform-token:tokens:write platform-token:tokens:manage"
_PLATFORM_TOKEN_API = "{api_host}/iam/v1/accounts/{account_id}/platform-tokens"


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
        account_api_host: str = "",
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
        # Account Management API, for `kind: platform` specs only. Defaults per realm;
        # the labs hosts share no stem with the tenant, hence the override.
        self._account_api_host = (account_api_host or account_api_for(self.tenant_url)).rstrip("/")
        self._platform_bearer: Optional[str] = None
        self._platform_bearer_expiry: Optional[datetime] = None

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
                          r.status_code, scrub_for_log(url),
                          scrub_for_log(r.text, limit=300))
            r.raise_for_status()
            data = r.json()
            self._bearer = data["access_token"]
            # Conservative expiry: shave 60s off expires_in
            expires_in = int(data.get("expires_in", 3600))
            self._bearer_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
            log.debug("Refreshed OAuth2 bearer token (expires_in=%ds)", expires_in)

    @property
    def can_mint_platform(self) -> bool:
        """Whether ``kind: platform`` specs can be served by this credential.

        The Account Management API only accepts an account-scoped OAuth bearer, so a
        provisioner built from a bare ``api_token`` cannot mint one however wide that
        token's scopes are.
        """
        return bool(self._oauth_client_id and self._oauth_client_secret and self._oauth_resource)

    @property
    def _account_id(self) -> str:
        return self._oauth_resource.split(":")[-1]

    async def _platform_auth_headers(self) -> dict[str, str]:
        """Bearer for the Account Management API — a different grant from the
        environment one in :meth:`_auth_headers`, with its own scopes and lifetime."""
        now = datetime.now(timezone.utc)
        if self._platform_bearer and self._platform_bearer_expiry and now < self._platform_bearer_expiry:
            return {"Authorization": f"Bearer {self._platform_bearer}",
                    "Content-Type": "application/json"}

        url = self._oauth_token_url or await discover_token_url(self.tenant_url)
        self._oauth_token_url = url
        form = {
            "grant_type": "client_credentials",
            "client_id": self._oauth_client_id,
            "client_secret": self._oauth_client_secret,
            "scope": _PLATFORM_TOKEN_SCOPES,
            "resource": self._oauth_resource,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, data=form)
            if r.status_code >= 400:
                # SSO hard-400s an unheld scope and leaves error_description EMPTY, so
                # the body is all the caller gets. Say which scopes were asked for —
                # otherwise this is indistinguishable from a bad secret.
                log.error("Platform-token grant HTTP %s at %s (scopes=%r): %s",
                          r.status_code, scrub_for_log(url), _PLATFORM_TOKEN_SCOPES,
                          scrub_for_log(r.text, limit=300))
            r.raise_for_status()
            data = r.json()
            self._platform_bearer = data["access_token"]
            expires_in = int(data.get("expires_in", 300))
            self._platform_bearer_expiry = now + timedelta(seconds=max(expires_in - 30, 30))

        return {"Authorization": f"Bearer {self._platform_bearer}",
                "Content-Type": "application/json"}

    async def _create_platform_token(
        self, spec: TokenSpec, name: str, expires_iso: str,
    ) -> tuple[str, str]:
        """Mint one gen3 platform token. Returns ``(token_value, token_id)``."""
        headers = await self._platform_auth_headers()
        url = _PLATFORM_TOKEN_API.format(api_host=self._account_api_host,
                                         account_id=self._account_id)
        payload = {
            "name": name,
            # Platform scopes are already in gen3 vocabulary — pass through untouched.
            "scope": list(spec.scopes),
            "resource": [f"urn:dtenvironment:{environment_id(self.tenant_url)}"],
            "tags": ["enablement"],
            "expirationDate": expires_iso,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"platform token '{name}' returned no token value")
        return token, (data.get("tokenId") or data.get("id") or "")

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
                try:
                    if spec.kind == "platform":
                        # A different API, a different grant, and a hard prerequisite:
                        # refuse loudly rather than mint a classic token whose gen3
                        # scope names would be silently dropped in translation.
                        if not self.can_mint_platform:
                            raise RuntimeError(
                                f"'{spec.env_var}' is declared kind: platform, which needs an "
                                f"account OAuth client (client id + secret + account URN). This "
                                f"provisioner was built from "
                                f"{'an api_token' if self._api_token else 'a client with no account URN'}."
                            )
                        value, token_id = await self._create_platform_token(spec, name, expires_iso)
                    else:
                        r = await client.post(token_api, headers=headers, json={
                            "name": name,
                            "expirationDate": expires_iso,
                            "scopes": spec.scopes,
                        })
                        r.raise_for_status()
                        data = r.json()
                        value, token_id = data["token"], data["id"]

                    env[spec.env_var] = value
                    for alias in spec.aliases:
                        env[alias] = value
                    if token_id:
                        token_ids.append(token_id)
                    log.info("Created %s token '%s' (id=%s, expiry=%s)",
                             scrub_for_log(spec.kind), scrub_for_log(name),
                             scrub_for_log(token_id), expires_iso)
                except httpx.HTTPStatusError as exc:
                    msg = f"Failed to create token '{name}': HTTP {exc.response.status_code} — {exc.response.text[:200]}"
                    log.error("%s", scrub_for_log(msg, limit=400))
                    errors.append(msg)
                except Exception as exc:
                    msg = f"Failed to create token '{name}': {exc}"
                    log.error("%s", scrub_for_log(msg, limit=400))
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
        """Revoke all provisioned tokens. Best-effort — logs but does not raise.

        A token id carries its own family in its prefix (``dt0c01…`` classic,
        ``dt0s16…`` platform), and the two are deleted from different APIs with
        different bearers. Routing on the prefix means callers do not have to remember
        which spec produced which id — a job record only ever stored the flat list.
        """
        if not token_ids:
            return

        classic_ids = [t for t in token_ids if not t.startswith("dt0s16")]
        platform_ids = [t for t in token_ids if t.startswith("dt0s16")]

        async with httpx.AsyncClient(timeout=15) as client:
            if classic_ids:
                headers = await self._auth_headers()
                token_api = self.token_api
                for tid in classic_ids:
                    await self._revoke_one(client, f"{token_api}/{tid}", headers, tid)

            if platform_ids:
                if not self.can_mint_platform:
                    # Say so rather than fail silently: these tokens outlive the session
                    # and count against the per-owner cap that caused an outage before.
                    log.warning("Cannot revoke %d platform token(s) %s — this provisioner "
                                "has no account OAuth client", len(platform_ids), platform_ids)
                    return
                headers = await self._platform_auth_headers()
                base = _PLATFORM_TOKEN_API.format(api_host=self._account_api_host,
                                                  account_id=self._account_id)
                for tid in platform_ids:
                    await self._revoke_one(client, f"{base}/{tid}", headers, tid)

    @staticmethod
    async def _revoke_one(client: httpx.AsyncClient, url: str,
                          headers: dict[str, str], tid: str):
        try:
            r = await client.delete(url, headers=headers)
            if r.status_code in (200, 204, 404):
                log.info("Revoked token %s (status=%d)", tid, r.status_code)
            else:
                log.warning("Unexpected status revoking token %s: %d", tid, r.status_code)
        except Exception as exc:
            log.warning("Could not revoke token %s: %s", tid, exc)
