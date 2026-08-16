# Astroshop sessions came up empty — `bootstrapWorkshop` had never run

**Found** 2026-08-14, while trying to measure what an Astroshop session costs.
**Fixed** 2026-08-16. Kept because the shape of this bug is worth remembering: it
survived for as long as it did by looking exactly like success.

## What happened

Provision `astroshop-problems` through Orbital or through the app. The session comes
up, `postCreateCommand` finishes in **41 seconds**, and the log ends with:

```
Set DT_ENVIRONMENT, DT_API_TOKEN, DT_PLATFORM_TOKEN as Codespace secrets
or run 'bootstrapWorkshop' manually.
```

The dev container is real, k3d is up, and the Astroshop demo is **not deployed**. The
learner is told to set Codespace secrets, which they cannot do — this is an arena
session, not a Codespace they own.

## Why

`demo-astroshop-problems/.devcontainer/post-create.sh:50` gates the whole bootstrap:

```bash
if [ -n "$DT_ENVIRONMENT" ] && [ -n "$DT_API_TOKEN" ] && [ -n "$DT_PLATFORM_TOKEN" ] ...
  bootstrapWorkshop
else
  printInfoSection "Workshop bootstrap is opt-in"
```

Nothing that provisions a session supplied those two variables:

| Minter | Produced |
|---|---|
| Orbital `provisioning/token_specs.DEFAULT_SPECS` | `DT_OPERATOR_TOKEN`, `DT_INGEST_TOKEN` |
| App `api/mintTrainingTokens.function.ts` `DEFAULT_SPECS` | + `DT_ONEAGENT_TOKEN` |
| App `PLATFORM_SPECS` (gen3 tenants) | one token aliased to those same three |

`DT_API_TOKEN` and `DT_PLATFORM_TOKEN` appeared in **none** of them. The repo had no
`.devcontainer/yaml/dt-tokens.yaml` either, so `load_token_specs` fell through to the
framework default and the mismatch was never visible.

Consequence: **every** Astroshop session ever delivered this way was an empty dev
container. Nothing errored, nothing retried, and the session looked healthy on the
board — which is why it survived.

## Why it took three changes to fix

The obvious fix — add the two tokens to the framework default — is wrong twice over:
it would mint them for every training that does not want them, and it cannot express
what this workshop actually needs, which is **two token families in one session**.

| Consumer | Needs | Because |
|---|---|---|
| SDLC event helpers, CI ingest, credential vault | classic `dt0c01` | ingest + config endpoints |
| monaco, dtctl | gen3 `dt0s16` | the platform Documents/Workflows APIs refuse a classic Api-Token whatever its scopes |

So `TokenSpec` grew a `kind` (`classic` \| `platform`), and the three places that mint
learned to honour it:

1. **`demo-astroshop-problems#39`** — the repo declares all four tokens it needs.
   One source of truth, in the repo, where the consumer lives.
2. **`codespaces-framework@4ef612c`** — `TokenSpec.kind`/`aliases`;
   `DTTokenProvisioner` serves `kind: platform` through the Account Management API
   and *refuses loudly* when built from a credential that cannot reach it;
   `revoke_tokens` routes each id to its own API by prefix.
3. **`dynatrace-app-enablements#79`** — the app is what mints for real learners and
   had no way to ask what the repo declared. It now does
   (`GET /api/arena/trainings/{id}/token-specs`), and mints mixed sets.

**Keeping the answer in the repo rather than copying the table into the app is the
whole point.** The copy is what hid this.

## Two traps found on the way

**A classic API drops unrecognised scopes rather than rejecting them.** Sending gen3
scope names to `createApiToken` would have produced a token that authenticates
perfectly and can do nothing — a strictly worse failure than the one being fixed.
Hence `kind: platform` skips classic routing entirely rather than being translated.

**A 201 is not evidence a token works.** A platform token's effective permissions are
`scopes ∩ the OWNER's IAM policy`, and the mint API never checks the owner side. Both
halves were therefore probed against endpoints matching their *granted* scopes:

| Token | Probe | Result |
|---|---|---|
| classic | `POST /api/v2/bizevents/ingest` | **202** |
| classic | `GET /api/config/v1/autoTags` (`ReadConfig`) | 200 |
| classic | `GET /api/v2/credentials` (`credentialVault.read`) | 200 |
| platform | `GET /platform/document/v1/documents` | 200 |
| platform | `GET /platform/automation/v1/workflows` | 200 |

(`GET /api/v2/apiTokens` 400s on SRO — classic token *listing* is retired on that
environment. Not a token defect, and not on any path the workshop uses.)

## The account scope that was blocking it

`DT_PLATFORM_TOKEN` needs `platform-token:tokens:write` on the account OAuth client.
On 2026-08-14 the SRO client 400'd on that scope and this was recorded as blocked on
an account-admin change. **It was granted, and re-probing on 2026-08-16 returns 200.**

`environment-api:api-tokens:write` still 400s on that client, and does not need to:
classic minting goes through the platform proxy with `SRO_MINTER_PLATFORM_TOKEN`, or
through the app's own AppEngine identity.

**Never infer either from the tenant URL** — classic retirement is per *environment*,
not per domain or account.

## Consequence for capacity planning

Before the fix, Astroshop measured **495 MiB per session at 1.4% CPU** — a third of
what Kubernetes-101 costs, and meaningless: it was measuring an empty container. This
is the reason the plateau detector exists in `tools/capacity/measure_repo.py`; a fixed
timer cannot tell "settled" from "never started".

`bootstrapWorkshop` is a **20–25 minute** run (Dynatrace operator, Astroshop, a GitLab
helm install, seeded repos, monaco, loadgen, four release rolls), so a real
measurement has to wait for a plateau, not a timer.

Until a measured figure is published to `repo:units`, Astroshop stays at the
**unprofiled default of 3 units** (6 seats on an m6a.4xlarge) — the fail-safe working
as designed: an unmeasured training is priced as the heaviest thing we know, so the
error is a worker running below capacity rather than a workshop that oversells and
fails in front of a room.
