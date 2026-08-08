# Orbital — what credential do I need to register/run a tenant?

One place that answers: **Platform token or OAuth client? Tenant- or account-level?
Which scopes?** — for every operation Orbital performs on a target tenant.

> **Standing rule for our own tenants (COE / SRO / sprint), set 2026-08-07:** app deploys use that
> tenant's **account OAuth client** (`COE_CLIENT_*`, `SRO_CLIENT_*`, `SPRINT_CLIENT_*` in
> `/home/ops/.env`) via `POST /api/deploy/token` with `"token":""`. **No agent deploys with a
> platform token** — those are API-testing credentials only, and any deploy-shaped need for one
> gets asked to the user first. Customer tenants without an OAuth client keep the paste flow below.

---

## END STATE (2026-07-31) — the app holds the credential, Orbital stores NOTHING

The design has moved to **app-held minting**. Read this first; the sections below are the
per-generation reference.

1. **Bootstrap (once):** the tenant admin pastes an account OAuth client in Orbital's
   Register Tenant tab. Orbital mints one deploy bearer, installs the app, and **discards
   every credential** — the client is never written to Redis, disk, or logs. (Enforced by a
   test: `test_oauth_bootstrap_stores_nothing`.)
2. **Configure (once):** the admin pastes the same client **into the app** (Admin → Token
   minting → the `mintCredentials` app function). It is stored in the app's own **app-state**
   on that tenant (admin-ACL, function-readable, tenant-local). The app verifies the client
   can mint (account platform-token scope + environment ActiveGate scope) before storing.
3. **Steady state:** the **app** — not Orbital — mints per-user platform tokens (account API,
   env-scoped, short-TTL, tagged) and DynaKube ActiveGate tokens for each training session,
   and revokes them at session end. Orbital only ever receives the resulting token **values**
   (`ArenaProvisionRequest.dtEnv`). **Orbital holds no tenant credential at any point.**
4. **Self-update:** the app mints a short-lived, install-scoped bearer from its stored client
   and hands only that to Orbital, which builds + deploys the new version and discards it. The
   client secret never leaves the tenant; app-state persists across versions.

**Why this is safe:** a full Orbital compromise exposes **no** tenant credential — there is
nothing at rest to steal. The secret lives only on its own tenant, reachable only by an admin
there, and only ever leaves as ephemeral, purpose-scoped bearers. See the threat model below.

**Verified live 2026-07-31:** the app-held mint sequence (account bearer → platform token
mint → ActiveGate token → install bearer) succeeds on sprint with a real client. Classic
self-mint (app's own identity) verified end-to-end through the app UI on SRO
(`tokenProvisioned: true`). Platform mint verified end-to-end via the training-test runner on
sprint for k8s-101 (operator+ingest dt0s16 + AG dt0g02) and dtwiz-101 (full pass, 101s).

**Legacy note:** the per-domain `MINT_*_<DOMAIN>` env clients on Orbital remain ONLY for the
tenants we operate (COE / SRO / sprint). For customer/SE tenants there is no Orbital-held
credential — the app self-mints.

---

## RECOMMENDATION (decision): one account-level OAuth client, one implementation

Ask for **a single account-level OAuth client** (per account) — not platform tokens — and
use it for everything. One client is granted **both** (least privilege, all confirmed live):

| Level | Permission | For |
|---|---|---|
| Environment | `app-engine:apps:install` | install / upgrade the app |
| Environment | `app-engine:apps:run` | run the app's functions |
| Environment | `settings:objects:read` + `settings:objects:write` | remote-grail (analytics forwarding) + outbound allowlist |
| Environment | `environment-api:activegate-tokens:write` (+ `:create`) | **mint DynaKube's ActiveGate token per K8s session** — without it every training with an operator fails at ActiveGate. The app mints this env-scoped from the client at provision time |
| Environment | `app-engine:apps:delete` *(optional)* | Undeploy only |
| Account | `platform-token:tokens:write` + `platform-token:tokens:manage` | mint + revoke per-user platform tokens via the Account Management API (`POST {api}/iam/v1/accounts/{uuid}/platform-tokens`) — **the only identity type that API accepts; every dt0s16 platform token is rejected** |

**Deliberately NOT required** (drop from an over-broad client): `environment-api:api-tokens:write/read`
(classic apiToken creation — on gen2 the app self-mints with its *own app identity* via the
manifest scope `environment-api:api-tokens:write`, never the OAuth client; on gen3 it's
disabled), `environment-api:activegate-tokens:read` (we never list AG tokens),
`settings:objects:admin` (read/write suffice; admin only deletes *others'* objects). Granting
these widens the blast radius for no functional gain.

At call time Orbital does `client_credentials` against the realm SSO with the right
`resource`: the **account (`urn:dtaccount:…`)** for both deploy and minting. Orbital
already has this machinery (COE auto-deploy + sprint mint, both proven live).

### Self-serve rollout (implemented 2026-07-31)

The Register Tenant tab now has an **Account OAuth client** form (`POST /api/deploy/oauth`):
the tenant admin pastes tenant URL + client id + secret + `urn:dtaccount:<uuid>`, Orbital

1. mints a deploy bearer (`app-engine:apps:install app-engine:apps:run` + `settings:objects:*`,
   falling back to the minimal set and warning when the client lacks settings),
2. deploys/upgrades the app, sets remote-grail + outbound allowlist, registers content,
3. probes the mint scopes (`platform-token:tokens:write platform-token:tokens:manage`) —
   when granted and "enable token minting" is checked, stores the client **Fernet-encrypted**
   in Redis (`deploy:mintclient:{tenant_id}`).

Arena/training provisioning (`_tenant_platform_provisioner` in `dashboard/app.py`) prefers
that registered client over the per-domain `MINT_*` env clients, so any tenant registered
this way mints + revokes short-lived per-user platform tokens (scopes from the repo's
`dt-tokens.yaml`, classic→platform translation in `provisioning/token_specs.py`) with no
Orbital config change. Realm SSO + Account-API hosts default by domain
(`SSO_TOKEN_URL_BY_DOMAIN` / `ACCOUNT_API_BY_DOMAIN` in `app_deploy.py`), overridable per
request (`ssoUrl` / `apiHost`).

### Why the client needs NO Grail / data scopes

- **App runtime reads/writes** (DQL verification, leaderboards, documents, state) run with
  the **app's manifest scopes + the calling user's grants** (`app.config.json`), not the
  OAuth client. The client only installs the app.
- **Training-session ingest** (operator/ingest) is carried by the **minted** per-user
  platform tokens. The Account API lets the mint client create tokens with scopes the
  client itself does not hold (verified live 2026-06-30) — so the client stays at
  `platform-token:tokens:write/manage` and nothing more.
- **Remote grail** is two halves: *writing the setting into the tenant* = `settings:objects:write`
  (client has it); *the COE-side read token* = credential B, held separately — never the client.

### External connections opened at install (disclosed in the UI)

When the tenant enforces an app-function outbound allowlist (sprint/dev; prod usually
doesn't), `_ensure_outbound_allowlist` ADDS (never removes/tightens):

| Host | Why |
|---|---|
| `autonomous-enablements.whydevslovedynatrace.com` | Orbital — training content, session provisioning, live sessions |
| `raw.githubusercontent.com`, `api.github.com` | training content + images |
| `wwse.apps.dynatrace.com` | central enablement analytics (nam

**Why one client, one implementation:** the app's self-mint scope
`environment-api:api-tokens:write` is **deprecated**, and classic token creation is being
retired tenant-by-tenant. If we use **Account Management minting for every generation**, the
gen2/gen3 split disappears — one mint path for all tenants. Platform tokens would force
**two** implementations (gen2 in-tenant self-mint vs gen3 account mint). So: **OAuth client +
Account Management mint = one implementation that works everywhere.** The per-generation
platform-token notes below are the fallback/legacy reference.

---

## THREAT MODEL — what an Orbital compromise means for a registered tenant

**Read this before rolling out to customer/prospect tenants.** When a tenant enables token
minting, Orbital holds that tenant's account OAuth client (Fernet-encrypted in Redis, key
`GH_OAUTH_ENC_KEY` in `/home/ops/.env`). Be honest about the blast radius.

### The dangerous scope is `platform-token:tokens:write`, and it is effectively account-admin

The Account Management API **does not restrict a minted token's scopes to what the minting
client holds** — that is *by design* (it's how a client with only `platform-token:tokens:write`
mints an operator token carrying `storage:metrics:write`). Consequence: an attacker who
extracts the client can mint a platform token with **any scope in the account**, on **any
environment in that account** (the mint call takes `resource:[urn:dtenvironment:*]` as a
free parameter). So the minting client is **not** least-privilege in effect — it is a key to
the whole account.

**A hacker with the client + secret + account URN could:**
- Mint a token with `storage:logs:read` / `storage:*:read` and **read all customer Grail
  data** (logs, spans, metrics, entities) — not just enablement data.
- Mint `settings:objects:write` and **reconfigure the tenant** (alerting, security rules,
  data retention, OpenPipeline).
- Mint `app-engine:apps:install` and **install arbitrary apps**.
- Do this on **every environment in the account**, not only the one that registered.
- Mint **long-lived** tokens (expiry is attacker-chosen) that survive after the breach.

The deploy-only environment scopes (`app-engine:apps:install/run`, `settings:objects:*`) are
comparatively bounded; **the account mint permission is the crown jewel.** Deleting the OAuth
client in myaccount instantly revokes all of it — that is the customer's kill switch.

### Mitigations in place
- Client stored Fernet-encrypted (not plaintext); never logged; API returns client_id only.
- Minted **session** tokens are env-scoped (`urn:dtenvironment:{env_id}`) + short-lived
  (4h) + tagged `enablement` + revoked on terminate — the *legitimate* use is tightly bound.
- nginx allows the deploy endpoints signed-out but they only *use* a supplied credential;
  the stored client is reachable only via provisioning code, not any read endpoint.

### Mitigations NOT yet in place (do before customer GA)
- **Encryption key + Redis on the same host** — an attacker with Orbital root gets both the
  ciphertext and `GH_OAUTH_ENC_KEY`. A KMS / HSM-held key, or a per-tenant key the customer
  controls, would stop plaintext extraction from a host compromise.
- **No scope ceiling on the account side** — ask Dynatrace whether a mint client can be
  capped to a *fixed scope allowlist* (mint only these N scopes) and *fixed environment*.
  If yes, that collapses the blast radius from "account admin" to "can mint operator/ingest
  on one env." **This is the single most important ask.**
- **Egress/anomaly monitoring** on the mint path (alert on scopes/resources outside the
  enablement set).

### The lower-blast-radius alternative — app-held client, not Orbital-held
Move the OAuth client into the **app's own encrypted app-state on the tenant**; app functions
call SSO + Account API directly and pass minted token *values* to Orbital (the
`ArenaProvisionRequest.dtEnv` path already exists). Then **Orbital holds no tenant
credential** — a single Orbital compromise exposes *nothing*; each tenant's secret lives only
in that tenant. Trade-off: the secret sits in tenant app-state (whoever can invoke the admin
app functions there can use it) and app functions need `sso.dynatrace.com` + the account API
host on their outbound allowlist. This is arguably the correct end-state for customer tenants;
the Orbital-held model is fine for tenants we own (COE/SRO/sprint).

---

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
