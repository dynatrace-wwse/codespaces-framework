# The arena `oauthClientId` provisioning path — three defects, all fixed

**Found** 2026-08-14, while trying to provision a real Astroshop session to measure it.
**Fixed** 2026-08-14, same day, and verified with a live mint/revoke round trip on SRO.

This path had never worked. It only runs when a caller supplies
`oauthClientId`/`oauthClientSecret` — in normal operation the **app** mints with its own
identity and passes token *values* in `dtEnv`, taking a different branch. So it was
effectively dead except to scripted callers: a load test, a measurement harness, or
anything driving Orbital without the app in front of it. Which is exactly what the
capacity work needed.

## 1. The grant was posted to the tenant, not to SSO

```python
_OAUTH_TOKEN_URL = "{tenant}/sso/oauth2/token"
```

SSO does not live on the tenant. It is `https://sso.dynatrace.com` for production tenants
and `https://sso-sprint.dynatracelabs.com` for sprint. The tenant answered 301:

```
Redirect response '301 Moved Permanently' for url
'https://sro97894.apps.dynatrace.com/sso/oauth2/token'
```

`api_arena_provision` then returned `tokenProvisioned: false` with no `DT_OPERATOR_TOKEN`,
and the session died at `postCreateCommand` a few seconds later.

**Fix:** resolution moved to `provisioning/sso.py`, shared with `dashboard/app_deploy.py`,
which had the correct implementation all along. It asks the tenant
(`HEAD /platform/oauth2/authorization/dynatrace-sso`) and falls back to a domain-suffix
map — so a labs tenant cannot silently fall back to the production realm, which is what
the old single fallback would have done.

## 2. The token API endpoint was wrong for every credential we hold

There are two endpoints for creating a classic API token, and they accept **different**
credentials:

| Endpoint | Auth | Works with |
|---|---|---|
| `{live}/api/v2/apiTokens` | `Api-Token dt0c01…` | a classic API token |
| `{tenant}/platform/classic/environment-api/v2/apiTokens` | `Bearer` | a `dt0s16` platform token, or an OAuth client bearer |

The provisioner always used the first. A platform token gets `401 Token exchange failed`
there and `201` on the proxy. Sending the right token to the wrong endpoint looks exactly
like a permissions problem, which is why this survived so long.

**Fix:** `DTTokenProvisioner.token_api` chooses from the credential rather than from
config, and `_auth_headers` picks `Api-Token` vs `Bearer` the same way.

## 3. Account-scoped clients could not send their `resource`

`client_credentials` against an account client needs the account URN as `resource`;
app-installed clients have none and must not send an empty string. There was no way to
pass one.

**Fix:** optional `oauth_resource`, threaded through `ArenaProvisionRequest.oauthResource`
and stored in the job record so terminate can still revoke.

## What this does NOT fix: which credential can mint

Fixing the plumbing exposed a separate fact worth writing down, because it will look like
a regression to whoever hits it next.

**Neither the SRO nor the sprint account OAuth client can mint learner tokens.** Probed
directly against SSO on 2026-08-14:

| Client | `settings:objects:write` | `app-engine:apps:install` | `environment-api:api-tokens:write` | `platform-token:tokens:write` |
|---|---|---|---|---|
| SRO account | 200 | 200 | **400** | — |
| sprint mint | — | — | **400** | 200 |

SSO hard-400s any scope the client does not hold — there is no partial grant — so a mint
attempt fails at the grant, before any endpoint is touched.

The credential that **does** work on SRO is `SRO_MINTER_PLATFORM_TOKEN` (a `dt0s16`), used
as a Bearer against the platform proxy. Verified end to end: minted `DT_OPERATOR_TOKEN` +
`DT_INGEST_TOKEN` from the real `token_specs` for `enablement-kubernetes-101`, then
revoked both.

So a measurement harness should pass `api_token=$SRO_MINTER_PLATFORM_TOKEN`, not an OAuth
client. The OAuth path is now correct and will work the moment it is given a client that
holds `environment-api:api-tokens:write` — which is what a self-registered tenant stores
as its own `mint-client`.
