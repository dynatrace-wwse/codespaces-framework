# Orbital — what credential do I need to register/run a tenant?

One place that answers: **Platform token or OAuth client? Tenant- or account-level?
Which scopes?** — for every operation Orbital performs on a target tenant.

## Two distinct credentials (don't conflate them)

| Credential | Who holds it | Purpose |
|---|---|---|
| **A. Per-tenant register/deploy token** | pasted at Register Tenant (used once, never stored) | install the app, write the remote-grail + outbound-allowlist settings, grant the app its self-mint scope |
| **B. COE remote-grail token** | held **encrypted in Orbital** (`REMOTE_GRAIL_COE_TOKEN_ENC`, Fernet) | the COE-side token the app uses to forward/read training bizevents — set into each tenant's remote-grail setting by `_ensure_remote_grail`. **Not** pasted per tenant. |

This doc is about **A** (what you create + paste). B is created once on COE: a platform
token with `storage:events:write` + `storage:bizevents:read` + `storage:buckets:read`.

## Operations A must cover
1. **Deploy/undeploy the app** — `dt-app deploy` → AppEngine registry.
2. **Add remote-grail config + outbound allowlist** — classic Settings API v2.
3. **Let the app self-mint training tokens** — at install, grant the app the
   token-create scope it uses for hands-on labs (operator/ingest tokens).
4. *(optional)* **Read documents** — only if you want Orbital to verify imported content.

## What to create — by tenant generation

### Gen2 / classic-token tenants (most prod: `*.apps.dynatrace.com`, e.g. geu80787, sro97894)
**A Platform token created IN the target tenant** (Settings → Platform tokens), scopes:

| Scope | For |
|---|---|
| `app-engine:apps:install` | install / upgrade |
| `app-engine:apps:run` | run app functions |
| `app-engine:apps:delete` | undeploy only |
| `settings:objects:read` + `settings:objects:write` | remote-grail config + outbound allowlist (classic settings API) |
| `api-tokens:tokens:read` + `api-tokens:tokens:write` | grant the app its `environment-api:api-tokens:write` self-mint scope at install (so hands-on labs can mint operator/ingest tokens) |
| `document:documents:read` *(optional)* | content verification |

The app then **self-mints** training tokens with its own installed identity — classic
API-token creation still works on these tenants.

> ⚠️ **`environment-api:api-tokens:write` is now DEPRECATED** (shown as deprecated in the
> OAuth-client scope picker). That is the scope the app's self-mint relies on. So the gen2
> path above is on borrowed time — as classic token creation is retired tenant-by-tenant
> (sprint already, prod following), **the Account Management platform-token path becomes
> the direction for ALL generations**, not just gen3. Treat the gen2 self-mint as legacy;
> prioritise the Account Management mint (below) as the long-term mechanism.

### Gen3 / migrated tenants (sprint: `*.sprint.apps.dynatracelabs.com`, e.g. ydi9582h; rolling out to prod)
Classic API-token **creation is disabled** here (Settings API returns 400 "only available
in Account Management" — see `dynatrace-app-enablements/docs/sprint-mint-platform-tokens-spike.md`).
So token minting can't go through the tenant. You need **two** things:

1. **The same per-tenant Platform token as gen2** (deploy + settings) — *minus* the
   `api-tokens` scopes (no effect here).
2. **An account-level OAuth client** (myaccount.dynatrace.com → Identity & access
   management → OAuth clients, in the tenant's **account**) authorized for **token
   management**, used against the Account Management API (`api.dynatrace.com`) to mint
   platform tokens for trainings. Orbital holds it **encrypted** (like B) and brokers
   mint/revoke. **Exact account scope: confirm in the OAuth-client UI** — it's in the
   `account-*` family (the create-client picker lists e.g. `account-idm-read/write`,
   `account-uac-read/write`); grant the token/identity-management one. Hand Orbital:
   `client_id`, `client_secret`, account `urn:dtaccount:<uuid>`. (The exact create
   endpoint/body is finalized once this client exists — the IAM base
   `api.dynatrace.com/iam/v1/accounts` is confirmed reachable.)

## How Orbital uses A at Register Tenant
`/api/deploy/token` (paste token) or `/api/deploy/start` (SSO): deploy → then
`_ensure_outbound_allowlist` + `_ensure_remote_grail` (both need `settings:objects:write`).
If the token lacks `settings:objects:write`, the deploy still succeeds but those steps are
**skipped** and reported in the deploy response/audit `warnings[]` (see app_deploy.py).

## Quick decision
- **Prod `.apps.dynatrace.com`** → one tenant Platform token (gen2 scope set). Done.
- **Sprint/dev `.sprint|dev.apps.dynatracelabs.com`** → tenant Platform token (deploy +
  settings) **plus** an account OAuth client for minting (gen3).
