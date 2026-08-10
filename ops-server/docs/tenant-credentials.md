# Orbital — what credential do I need to register/run a tenant?

One place that answers: **Platform token or OAuth client? Tenant- or account-level?
Which scopes?** — for every operation Orbital performs on a target tenant.

> **Standing rule for our own tenants (COE / SRO / sprint), set 2026-08-07:** app deploys use that
> tenant's **account OAuth client** (`COE_CLIENT_*`, `SRO_CLIENT_*`, `SPRINT_CLIENT_*` in
> `/home/ops/.env`) via `POST /api/deploy/token` with `"token":""`. **No agent deploys with a
> platform token** — those are API-testing credentials only, and any deploy-shaped need for one
> gets asked to the user first. Customer tenants without an OAuth client keep the paste flow below.

---

## END STATE (2026-08-10) — one paste, then the tenant is self-sufficient

The design has moved to **app-held minting**, and as of 2026-08-10 the configuration step is
automatic. Read this first; the sections below are the per-generation reference.

1. **Register (once, the only human action):** the tenant admin pastes an account OAuth client
   in Orbital's Register Tenant tab. Orbital mints one deploy bearer, installs the app, writes
   the outbound allowlist (**including the client's own SSO/account-API hosts**, so an unfamiliar
   realm is reachable) and remote-grail, **hands the client to the tenant** (step 2), and then
   **discards every credential** — nothing is written to Redis, disk, or logs. (Enforced by
   `test_oauth_bootstrap_stores_nothing` and `test_the_secret_never_reaches_redis_or_the_audit`.)
2. **Configure (automatic, inside step 1):** `_store_mint_client` writes the client into the
   app's own **`mint-client` settings object** on that tenant — through the *classic* settings
   API with `settings:objects:write`, the same door `_ensure_remote_grail` has always used.
   `clientSecret` is a `secret`-typed property: masked to every external reader, plaintext only
   to the app. Before storing, Orbital probes the account mint scopes and the environment
   ActiveGate scope and warns on each independently.

   > This step used to be a manual second paste, documented as impossible to automate because
   > `app-settings:objects:write` is not grantable to any OAuth client. That scope is indeed
   > ungrantable — and it is the wrong door. App settings and classic settings are the **same
   > objects**: measured on ydi9582h, the app's `remote-grail` object returns an identical
   > `objectId` through `/platform/classic/environment-api/v2/settings/objects?schemaIds=app:my.dynatrace.enablements:remote-grail`
   > and `/platform/app-settings/v2/objects?schema-id=remote-grail`.

   The legacy `mint:oauth-client` app-state key is still **read** as a fallback, so tenants
   configured the old way (ydi, COE, SRO) keep working with no migration. Settings is also the
   more durable home: an app uninstall **destroys** app-state and **keeps** settings.
3. **Steady state:** the **app** — not Orbital — mints per-user platform tokens (account API,
   env-scoped, short-TTL, tagged) and DynaKube ActiveGate tokens for each training session,
   and revokes them at session end. Orbital only ever receives the resulting token **values**
   (`ArenaProvisionRequest.dtEnv`). **Orbital holds no tenant credential at any point.**
   A tenant that *cannot* mint now **refuses to provision** and reports the SSO/scope error,
   instead of shipping an empty `DT_OPERATOR_TOKEN` that fails minutes later in the operator
   install.
4. **Self-update:** the app mints a short-lived, install-scoped bearer from its stored client
   and hands only that to Orbital, which builds + deploys the new version and discards it. The
   client secret never leaves the tenant; settings persist across versions.

   > Until 2026-08-10 the app posted `token: ''` unconditionally, which means "Orbital, use the
   > client *you* hold". Orbital holds three, so "Update now" worked on exactly COE, SRO and
   > ydi and answered every other tenant with an honest 400 (measured on bfs7010h,
   > `deploy:job:wESlPSHDPJ5ChO2j`). The tokenless post is still the fallback when a tenant has
   > no stored client, so those three are unaffected.

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

### Self-serve rollout (`POST /api/deploy/oauth`, current as of 2026-08-10)

The Register Tenant tab has an **Account OAuth client** form: the tenant admin pastes tenant
URL + client id + secret + `urn:dtaccount:<uuid>`, and Orbital

1. mints a deploy bearer (`app-engine:apps:install app-engine:apps:run` + `settings:objects:*`,
   falling back down a scope ladder and warning when the client lacks settings),
2. deploys/upgrades the app, sets remote-grail + outbound allowlist (with the **client's own**
   `ssoUrl`/`apiHost` added, so an unfamiliar realm is reachable), registers content,
3. probes the account mint scopes (`platform-token:tokens:write platform-token:tokens:manage`)
   and the environment ActiveGate scope (`environment-api:activegate-tokens:write`), warning
   on each independently,
4. **writes the client into the tenant's own `mint-client` settings object**
   (`_store_mint_client`) and then drops every credential it held.

Realm SSO + Account-API hosts default by domain (`SSO_TOKEN_URL_BY_DOMAIN` /
`ACCOUNT_API_BY_DOMAIN` in `app_deploy.py`), overridable per request (`ssoUrl` / `apiHost`).

**Orbital does not mint, and does not store a mint client.** PR #144 removed the last paths
that did: `_gen3_platform_provisioner`, the `api_arena_provision` fallback branch, the
cross-tenant workshop shortcut, `_sweep_leaked_platform_tokens`, `GET /api/deploy/mint-clients`
and `PlatformTokenProvisioner`. The `deploy:mintclient:{tenant_id}` Redis key no longer exists.
Every training token on a registered tenant is minted **by the app, with the tenant's own
client**, using scopes from the repo's `dt-tokens.yaml` (classic→platform translation in
`provisioning/token_specs.py`).

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

## THREAT MODEL — the OAuth client is account-admin in effect, wherever it lives

**Read this before rolling out to customer/prospect tenants.**

> **Orbital no longer holds it.** Since 2026-08-10 the client is stored on the **tenant** (a
> `secret`-typed property of the app's own `mint-client` settings object) and Orbital keeps
> nothing at rest — so the "Orbital compromise" half of this model is now moot, and what
> remains is the blast radius of the *permission itself*, which is unchanged and still the
> reason to be careful about who is asked to create one. The Orbital-held variant below is
> retained because it describes the risk the permission carries in any home.

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
- **The client lives only on its own tenant** (`mint-client` settings, `secret`-typed property:
  masked to every external reader, plaintext only to the app). Orbital holds it for the duration
  of one deploy call and never writes it anywhere — asserted by
  `test_the_secret_never_reaches_redis_or_the_audit`. A full Orbital compromise yields **no**
  tenant credential.
- The secret never leaves the tenant afterwards; it only ever exits as **ephemeral,
  purpose-scoped bearers** (mint / install).
- Minted **session** tokens are env-scoped (`urn:dtenvironment:{env_id}`) + short-lived
  (4h) + tagged `enablement` + revoked on terminate — the *legitimate* use is tightly bound.
- nginx allows the deploy endpoints signed-out but they only *use* a supplied credential;
  there is no read endpoint that returns one.
- **Kill switch:** deleting the OAuth client in myaccount instantly revokes everything above.

### Mitigations NOT yet in place (do before customer GA)
- **No scope ceiling on the account side** — ask Dynatrace whether a mint client can be
  capped to a *fixed scope allowlist* (mint only these N scopes) and *fixed environment*.
  If yes, that collapses the blast radius from "account admin" to "can mint operator/ingest
  on one env." **This is the single most important ask**, and it is now the only structural
  one left: moving the client to the tenant removed the others.
- **Egress/anomaly monitoring** on the mint path (alert on scopes/resources outside the
  enablement set).
- **At-rest honesty:** a value the app must itself use to authenticate cannot be
  envelope-encrypted against a tenant admin — the app would need the key, which then lives on
  the same tenant. The boundary is the settings ACL plus "Orbital holds nothing", not
  client-side crypto. We deliberately ship no key-in-bundle theatre.

### Implemented 2026-08-10 — app-held client (was "the lower-blast-radius alternative")
The OAuth client now lives in the **app's own `mint-client` settings object on the tenant**;
app functions call SSO + the Account API directly and pass minted token *values* to Orbital
(`ArenaProvisionRequest.dtEnv`). **Orbital holds no tenant credential** — a single Orbital
compromise exposes nothing. Residual trade-off, unchanged from when this was a proposal:
whoever can invoke the admin app functions on that tenant can use the client, and app functions
need the realm SSO + account API host on their outbound allowlist (which
`_ensure_outbound_allowlist` now adds automatically from the pasted client). This is the model
for **all** tenants, ours included.

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

## What to create — one answer, all generations

**An account OAuth client with the scope set in `dynatrace-app-enablements/docs/tenant-onboarding.md`.**
Register it once, and the tenant covers both mint paths and its own updates. There is no
gen2/gen3 decision to make any more, because the client works on both and the app picks the
path the environment allows.

### Which mint path the app takes, and what needs the client

| Path | Credential | Client needed? |
|---|---|---|
| Classic `dt0c01` | the app's **own installed AppEngine identity** (manifest scope `environment-api:api-tokens:write`) | **no** |
| Platform `dt0s16` | stored client → account bearer → `POST {apiHost}/iam/v1/accounts/{uuid}/platform-tokens` | **yes** |
| ActiveGate `dt0g02` | stored client → env-scoped bearer | **yes** |
| Self-update | stored client → env-scoped install bearer | **yes** |

Classic minting needing no client is why an unconfigured tenant looks *half* working — a
training starts, but gen3 minting and "Update now" both fail. That asymmetry is the fingerprint
of a tenant with no stored client.

### Classic-token retirement is per ENVIRONMENT, not per domain or per account

Measured 2026-08-10, in the **same** sprint account: `ydi9582h` created a `dt0c01` normally,
while `pvf2584h` answered
`400 "Creation of new tokens is now only available in Account Management."`
So "sprint = gen3, prod = gen2" is not a rule you can rely on, and neither is anything derived
from the account. Treat every environment as possibly retired: register the OAuth client and the
question stops mattering. (Background:
`dynatrace-app-enablements/docs/sprint-mint-platform-tokens-spike.md`.)

`environment-api:api-tokens:write` — the scope the classic self-mint relies on — is shown as
DEPRECATED in the scope picker, so the classic path is on borrowed time everywhere. The Account
Management platform-token path is the long-term mechanism for **all** generations.

## How Orbital uses the credential at Register Tenant
`/api/deploy/oauth` (client, preferred) or `/api/deploy/token` (paste, fallback): deploy → then
`_ensure_outbound_allowlist` + `_ensure_remote_grail` + `_store_mint_client` (all three need
`settings:objects:write`). If the credential lacks it, the deploy still succeeds but those steps
are **skipped** and reported in the response/audit `warnings[]` — and for `_store_mint_client`
that is raised as **ACTION REQUIRED**, since the tenant is then not self-sufficient.

## Quick decision
- **Any tenant, any domain, any generation** → one account OAuth client, registered through
  `/api/deploy/oauth`. Done.
- **Admin cannot create an OAuth client** → the platform-token paste flow in
  `dynatrace-app-enablements/docs/tenant-onboarding.md` § Fallback. Installs and updates by
  paste, classic minting works, gen3 minting and self-update do not.
