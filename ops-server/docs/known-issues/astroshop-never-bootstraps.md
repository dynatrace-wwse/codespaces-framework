# Astroshop sessions come up empty — `bootstrapWorkshop` has never run

**Found** 2026-08-14, while trying to measure what an Astroshop session costs.
**Status:** root-caused. Half fixable here, half blocked on an account-level scope.

## What happens

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

Nothing that provisions a session supplies those two variables:

| Minter | Produces |
|---|---|
| Orbital `provisioning/token_specs.DEFAULT_SPECS` | `DT_OPERATOR_TOKEN`, `DT_INGEST_TOKEN` |
| App `api/mintTrainingTokens.function.ts` `DEFAULT_SPECS` | `DT_OPERATOR_TOKEN`, `DT_INGEST_TOKEN`, `DT_ONEAGENT_TOKEN` |
| App `PLATFORM_SPECS` (gen3 tenants) | one token aliased to those same three |

`DT_API_TOKEN` and `DT_PLATFORM_TOKEN` appear in **none** of them. The repo has no
`.devcontainer/yaml/dt-tokens.yaml` either, so `load_token_specs` falls through to the
framework default and the mismatch is never visible.

Consequence: **every** Astroshop session ever delivered this way has been an empty
dev container. Nothing errors, nothing retries, and the session looks healthy on the
board — which is why it has survived.

## Fixing it

### `DT_API_TOKEN` — ready, verified mintable

Classic token. `post-create.sh` documents the scopes it needs, and all ten mint 201 on
SRO through the platform proxy (verified 2026-08-14):

```
ReadConfig  WriteConfig  events.ingest  bizevents.ingest
openpipeline.events_sdlc.custom  CaptureRequestData
credentialVault.read  credentialVault.write  apiTokens.read  apiTokens.write
```

The designed extension point is a `dt-tokens.yaml` in the **astroshop repo**:

```yaml
tokens:
  - name_suffix: operator
    env_var: DT_OPERATOR_TOKEN
    scopes: [activeGateTokenManagement.create, activeGateTokenManagement.write,
             entities.read, settings.read, settings.write, DataExport,
             InstallerDownload]
  - name_suffix: ingest
    env_var: DT_INGEST_TOKEN
    scopes: [metrics.ingest, logs.ingest, events.ingest, openTelemetryTrace.ingest]
  - name_suffix: api
    env_var: DT_API_TOKEN
    scopes: [ReadConfig, WriteConfig, events.ingest, bizevents.ingest,
             openpipeline.events_sdlc.custom, CaptureRequestData,
             credentialVault.read, credentialVault.write,
             apiTokens.read, apiTokens.write]
```

Not applied here: it is a change to another repo, and on its own it does not lift the
gate — see below. The app's `DEFAULT_SPECS` needs the same third entry, or the app
path stays broken while the Orbital path works.

### `DT_PLATFORM_TOKEN` — blocked on a scope nobody holds

A `dt0s16`, minted against the **account** API
(`api.dynatrace.com/iam/v1/accounts/{acct}/platform-tokens`) with an OAuth bearer
carrying `platform-token:tokens:write`. Probed directly against SSO, 2026-08-14:

| Client | `platform-token:tokens:write` |
|---|---|
| SRO account (`SRO_CLIENT_ID`) | **400 — not held** |
| sprint mint (`MINT_CLIENT_ID_SPRINT`) | 200 |

So Orbital cannot mint one for SRO at all. Granting that scope to the SRO account
client is an account-admin change and the smallest thing that unblocks the rest.

Worth asking separately whether the gate needs to be a conjunction: if
`DT_PLATFORM_TOKEN` only feeds the monaco/dtctl steps, a partial bootstrap with
`DT_API_TOKEN` alone would deliver most of the workshop instead of none of it.

## Consequence for capacity planning

Astroshop's steady-state cost **cannot be measured** until this is fixed — a run today
measures an empty container. Measured anyway, for the record: 495 MiB per session with
the workers at 1.4% CPU, which is a third of what Kubernetes-101 costs and is
meaningless as a planning figure.

It therefore stays at the **unprofiled default of 3 units** (6 seats on an
m6a.4xlarge). That is the fail-safe working as designed: an unmeasured training is
priced as the heaviest thing we know, so the error is a worker running below capacity
rather than a workshop that oversells and fails in front of a room.
