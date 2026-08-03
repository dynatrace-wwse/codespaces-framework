# EPIC-002 §9 — Workstream D: Tenant identity & attribution

**Question:** given a tenant the app is installed on (envId, or the OAuth client /
platform token it presented), can Orbital find out *who* owns it — an email, an
account name?

**Answer: no — not via any Dynatrace API.** Attribution must be captured at
deploy time and backstopped at runtime. That is what the tenant-attribution
registry implements (`dashboard/tenant_registry.py`).

## What is NOT retrievable via API (verified)

| Wanted | Why it's not available |
|---|---|
| Email of an OAuth client's owner | The Account Management API returns OAuth client metadata (id, scopes, description) but **no owner user/email**. Clients belong to the *account*, not a person. See [OAuth clients](https://docs.dynatrace.com/docs/manage/identity-access-management/access-tokens-and-oauth-clients/oauth-clients) and the [Account Management API](https://docs.dynatrace.com/docs/dynatrace-api/account-management-api). |
| Account *name* for an account URN | `urn:dtaccount:<uuid>` is all a client credential exposes; resolving it to a human-readable account name needs Account-Management read scopes **on that account** — which Orbital doesn't and shouldn't hold for foreign accounts. |
| envId → account mapping | There is no public cross-account directory. The [environment-management endpoints](https://docs.dynatrace.com/docs/dynatrace-api/account-management-api) only enumerate environments *within an account you can already access*. |
| Platform-token lookup-by-secret | The [Platform tokens API](https://docs.dynatrace.com/docs/manage/identity-access-management/access-tokens-and-oauth-clients/platform-tokens) has no "who does this token belong to" lookup; token metadata (owner) is only listable *from inside the owning tenant* with `platform-token:tokens:read`. |

## Solution (shipped in this workstream)

1. **Deploy-time registry** — every deploy call site in
   `dashboard/app_deploy.py` writes a Redis hash `tenant:registry:{tenant_id}`
   (+ index set `tenant:registry:index`) with `{accountUrn, clientId,
   deployerEmail, via, firstSeen, lastDeploy, appVersion}`.
   `via ∈ {sso-deploy, auto, token, oauth-bootstrap}`. The OAuth-bootstrap path
   already holds the accountUrn + clientId (previously discarded after the
   deploy) — now recorded (never the secret). The SSO path records the GitHub
   username as deployer. The Register Tenant form gained an optional
   "your email" field feeding `deployerEmail`.
2. **Runtime backstop** — `POST /api/tenants/register-identity`
   `{tenant, email, name, accountUrn}` (Orbital service bearer required): the
   app reports its admin's identity on first admin visit; fills
   `deployerEmail` if the deploy-time record left it empty, always refreshes
   `lastSeen` + identity fields.
3. **CoE list** — `GET /api/tenants/registry` (writer or service bearer):
   full, unmasked entries; surfaced as a "Tenants" table on the dashboard's
   Register Tenant tab.

`tenant_map.json` (content delivery) is intentionally untouched — separate
concern.
