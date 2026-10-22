# Functions Reference

`functions.sh` is the core library of the Dynatrace Enablement Framework. It is automatically sourced into every terminal session, making all functions available as shell commands. Functions are grouped below by area.

---

## Logging & Output

These functions produce colored, timestamped output to the terminal.

| Function | Description |
|---|---|
| `timestamp` | Returns the current date/time as `[YYYY-MM-DD HH:MM:SS]` |
| `printInfo <msg>` | Prints an INFO-level message in cyan/blue |
| `printInfoSection <msg>` | Prints a decorated section banner for major steps |
| `printWarn <msg>` | Prints a WARN-level message in yellow |
| `printError <msg>` | Prints an ERROR-level message in red |
| `printGreeting` | Prints the MOTD-style framework greeting |
| `printSecrets` | Displays all DT credentials (tokens masked to first 14 chars) |

All logging functions accept an optional second argument `false` to suppress output — useful when calling from other functions that capture stdout.

```bash
printInfoSection "Deploying my app"
printInfo "Connected to $DT_ENVIRONMENT"
printWarn "Token not set — defaulting to playground"
printError "Deployment failed — check kubectl events"
```

---

## Terminal & Shell Setup

| Function | Description |
|---|---|
| `setUpTerminal` | Installs Powerlevel10k, sources framework functions, and adds aliases to `.zshrc` |
| `setUpHostTerminal` | Converts a plain Ubuntu server to Zsh + Oh My Zsh + Powerlevel10k with aliases — no framework functions bound to the shell |
| `bindFunctionsInShell` | Appends the `source_framework.sh` loader and greeting call to `.zshrc` |
| `setupAliases` | Adds shell and `kubectl` convenience aliases to `.zshrc` |

`setUpTerminal` is called once during container creation. `setUpHostTerminal` is for standalone Ubuntu servers — run it once after cloning the framework:

```bash
source .devcontainer/util/source_framework.sh && setUpHostTerminal
exec zsh   # or log out and back in
```

`setUpHostTerminal` installs zsh and Oh My Zsh if missing, clones the Powerlevel10k theme, deploys the framework's `.zshrc` and `.p10k.zsh` configs, and appends aliases. It does **not** bind framework functions into the shell. Type `p10k configure` inside zsh to reconfigure the prompt.

Aliases added by `setupAliases`:

```
las     → ls -las          c       → clear
hg      → history | grep   h       → history
gita    → git add -A        gitc    → git commit -s -m
gitp    → git push          gits    → git status
gith    → git log --graph (pretty format)
pg      → ps -aux | grep
```

---

## Dynatrace Environment & MCP

### Environment Selection

| Function | Signature | Description |
|---|---|---|
| `selectEnvironment` | `selectEnvironment` | Interactive prompt to pick a DT environment; saves to `.env` and exports `DT_ENVIRONMENT` |
| `setEnvironmentInEnv` | `setEnvironmentInEnv` | Writes `DT_ENVIRONMENT` to `.env`; defaults to playground if unset |
| `parseDynatraceEnvironment` | `parseDynatraceEnvironment [url]` | Parses a DT URL and derives `DT_TENANT`, `DT_ENV_TYPE`, `DT_OTEL_ENDPOINT` |

```bash
# Select interactively
selectEnvironment

# Parse a specific URL
parseDynatraceEnvironment "https://abc123.apps.dynatrace.com"
# Exports:
#   DT_ENVIRONMENT  → https://abc123.apps.dynatrace.com
#   DT_TENANT       → https://abc123.live.dynatrace.com
#   DT_ENV_TYPE     → prod
#   DT_OTEL_ENDPOINT → https://abc123.live.dynatrace.com/api/v2/otlp
```

Supported environment types: `prod`, `sprint`, `dev`, `labs`, `custom`.

### Credential Validation

| Function | Signature | Description |
|---|---|---|
| `variablesNeeded` | `variablesNeeded VAR:true VAR:false ...` | Validates required/optional variables; validates DT token format and parses DT_ENVIRONMENT |
| `validateSaveCredentials` | `validateSaveCredentials [env token ingest]` | Validates and exports DT credentials |
| `dynatraceEvalReadSaveCredentials` | `dynatraceEvalReadSaveCredentials [env token ingest]` | Full evaluation pipeline: reads env vars or args, validates, exports, prints |
| `verifyParseSecret` | `verifyParseSecret <secret>` | **Deprecated** — use `parseDynatraceEnvironment` and `variablesNeeded` instead |

```bash
# Declarative validation in post-create.sh
variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:false

# Read credentials from environment or prompt
dynatraceEvalReadSaveCredentials

# Pass credentials explicitly
dynatraceEvalReadSaveCredentials "https://abc.apps.dynatrace.com" "$OP_TOKEN" "$INGEST_TOKEN"
```

`variablesNeeded` validates Dynatrace token format (`dt0c01.*` or `dt0s01.*`, minimum 60 characters) and automatically calls `parseDynatraceEnvironment` for `DT_ENVIRONMENT`.

### MCP Server (VS Code)

| Function | Description |
|---|---|
| `enableMCP` | Creates `.vscode/mcp.json` connecting VS Code to the Dynatrace MCP Server |
| `disableMCP` | Removes `.vscode/mcp.json` |
| `setupMCPServer` | **Deprecated** — use `enableMCP` |

```bash
enableMCP       # prompts for environment if DT_ENVIRONMENT is not set
disableMCP      # removes the MCP config
selectEnvironment && enableMCP  # switch environments and re-enable
```

MCP is opt-in. The generated `mcp.json` runs `@dynatrace-oss/dynatrace-mcp-server` via `npx` and reads credentials from `.devcontainer/.env`.

### dtctl & Monaco

| Function | Signature | Description |
|---|---|---|
| `installDevTools` | `installDevTools` | Run by `finalizePostCreation` in **every** repo: installs dtctl and Monaco at the framework's pinned versions, plus a read-only dtctl context when `DT_DTCTL_TOKEN` is set. Off with `FRAMEWORK_DEVTOOLS=off`. Never fatal — see [dtctl & Monaco](dtctl-monaco.md) |
| `installDtctl` | `installDtctl [dir]` | Installs dtctl only (no context), checksum-verified |
| `enableDtctl` | `enableDtctl [--readwrite] [--token-var NAME]` | Installs the latest [dtctl](https://github.com/dynatrace-oss/dtctl) (checksum-verified) and configures a context for `$DT_ENVIRONMENT`. Safety level `readonly` unless `--readwrite` — a client-side guard only: under Orbital the token itself can write and delete (see **Credentials** below) |
| `disableDtctl` | `disableDtctl` | Removes the dtctl binary, its config and any file-stored tokens |
| `installMonaco` | `installMonaco` | Installs [Monaco](https://github.com/Dynatrace/dynatrace-configuration-as-code) at `MONACO_VERSION`: for the framework's pin, the framework's [patched build](dtctl-monaco.md#monaco-is-a-patched-build) (reviewed sha256, never a stock fallback; a refusal removes any `monaco` left in the install dir); otherwise the upstream release (checksum-verified) |
| `deployMonaco` | `deployMonaco <tenant\|env>` · `deployMonaco -h` | Deploys the repo's `monaco/<phase>/` as a per-learner project. Runs only the verified patched Monaco (pinned sha256 or `+wwse.1` in `monaco version`); otherwise one `installMonaco`, then fails loudly |
| `undeployMonaco` | `undeployMonaco [tenant\|env]` · `undeployMonaco -h` | Deletes what `deployMonaco` created, from the delete file it persisted. No argument: both phases |
| `verifySha256` | `verifySha256 <file> <sha256>` | Returns 0 only on a match; a mismatch, a missing file or a malformed checksum all refuse |

Both binaries are already on the `PATH` in every environment (`installDevTools`, see
[dtctl & Monaco](dtctl-monaco.md)). Deploying config is still opt-in, from a repo's `post-create.sh`:

```bash
deployMonaco tenant      # early: tenant-only config (dashboards, notebooks, settings)
startCluster
deployTodoApp
deployMonaco env         # after the app answers
enableDtctl              # optional: learners inspect what was deployed
```

**Verified installs.** Both installers use the version in `DTCTL_VERSION` / `MONACO_VERSION` —
pinned by the framework in `variables.sh` — or, when that is empty, the latest GitHub release.
For the pinned version the download must match the reviewed sha256 committed in `variables.sh`
**and** the release's checksum file; see [dtctl & Monaco](dtctl-monaco.md#supply-chain). A
binary is kept only when its sha256 matches the pin and it reports the wanted version. dtctl's archive is checked against the release's
`checksums.txt`, Monaco's binary against its `.sha256`. A mismatch, or an asset missing from the
checksum file, refuses the install. The pinned Monaco is the framework's
[patched build](dtctl-monaco.md#monaco-is-a-patched-build), checked against the reviewed sha256
only; a mismatch refuses it and does **not** install stock Monaco instead. The checksum file comes from the same release, so this
proves the download is intact, not who published it; pin a reviewed version for a delivery.
A pin that is not `x.y.z` is refused before any download: nothing is written to `/usr/local/bin` (override with
`DTCTL_INSTALL_DIR` / `MONACO_INSTALL_DIR`). Architectures: `amd64` and `arm64`.

**dtctl context.** `enableDtctl` runs non-interactively:

| Setting | Value |
|---|---|
| context | `enablement` (`DTCTL_CONTEXT`) |
| environment | `$DT_ENVIRONMENT` |
| token | the literal reference `${DT_DTCTL_TOKEN}` (or `${NAME}` with `--token-var NAME`). dtctl expands it on every command; the value is never written to the config |
| safety level | `readonly`; `--readwrite` sets `readwrite-mine`. Safety levels are a **client-side** guard — they do not narrow what the token itself may do |
| storage | `DTCTL_TOKEN_STORAGE=file` (a container has no keyring) |

!!! warning "dtctl commands that expose the token"
    **Do not run `dtctl config view`** — it prints the **resolved** token. And **any**
    `dtctl config` write — `use-context`, or `set-credentials` for another context — rewrites
    the config with every `${VAR}` expanded, so the token value lands in the file (dtctl
    0.42–0.43). `enableDtctl` writes the credential last, refuses a config that holds the value,
    and leaves the config **read-only** (`0400`) so a later config write fails instead of
    leaking. To switch or repair the context, re-run `enableDtctl` — it restores the mode itself.

**Credentials.** Both tools read a token by variable **name**, never a pasted value: dtctl
`DT_DTCTL_TOKEN`, Monaco `DT_MONACO_TOKEN` (override with `MONACO_TOKEN_VAR`). Under Orbital
the Enablement app supplies them to every learner seat it builds — not the repo's
`.devcontainer/yaml/dt-tokens.yaml`: a spec there under either name is dropped. It is **one**
platform token (`dt0s16`) per seat, delivered under **both** names with the same value, carrying
the union of the two lists: `document:documents:read/write/delete`, `settings:objects:read/write`,
`settings:schemas:read` and `storage:logs/spans/events/bizevents/metrics/entities/buckets:read`.
So `DT_DTCTL_TOKEN` is **not** read-only: it can write and delete any document and change any
setting the minting client's owner can reach on the tenant; dtctl's `readonly` safety level is
the only thing that keeps `dtctl` from doing so. A seat can be built without the token (the
per-owner platform-token cap, a tenant with no registered OAuth client) — the app
says so on screen, and `installDevTools` then installs dtctl with no context. Outside Orbital,
set the variables yourself; Monaco needs a platform token — documents are platform-only.

#### The `monaco/` folder

```
monaco/
  tenant/          # deployed by `deployMonaco tenant` — tenant-only config, no app URL needed
    <any>/config.yaml + templates
  env/             # deployed by `deployMonaco env` — config that needs the running app
    <any>/config.yaml + templates
```

Each phase folder is one Monaco project (Monaco reads every `*.yaml` below it). The repo
ships **no manifest**: `deployMonaco` writes one at run time, so every learner deploys a
project of their own.

| What | Value | Why |
|---|---|---|
| project name | `<repo>-<DT_SESSION_ID>`, then `…-g<N>` after an undeploy left documents in the trash | Monaco derives settings and document ids from the project name. Two learners with the same project overwrite each other in a shared tenant. The `-g<N>` suffix: see "A trashed document keeps its id" |
| `LEARNER_PREFIX` | `<DT_SESSION_ID>` | Exported to Monaco. Use it in every `name:` so learners can find their own objects |
| environment | `learner`, URL from `DT_ENVIRONMENT`, `platformToken` from `DT_MONACO_TOKEN` | |

```yaml
# monaco/tenant/dashboards/config.yaml
configs:
- id: my-dashboard
  config:
    name:
      type: environment
      name: LEARNER_PREFIX     # e.g. "alice-20261013"
    template: dashboard.json
  type:
    document:
      kind: dashboard
```

`deployMonaco` refuses — non-zero, nothing deployed — when there is no `DT_SESSION_ID`
(`DT_HOSTGROUP` under Orbital, `GITHUB_USER` in a Codespace): without it every learner would
share one project.

**Supported types (v1): `document` (dashboards, notebooks) and `settings`.** Any other config
type — a classic API such as `synthetic-monitor` or `application-web`, `automation`, `bucket` —
is reported by file and refused with "not supported yet"; nothing is deployed. Those need a
classic token or IAM bindings that are not settled yet.

**Undo.** Before deploying, `deployMonaco` snapshots the phase folder into
`.devcontainer/.monaco/<phase>/` (git-ignored; override with `MONACO_RUN_DIR`) together with
the manifest and a delete file from `monaco generate deletefile`. If the delete file cannot be
generated, it does not deploy. A redeploy keeps the previous delete entries, so a config
removed from `monaco/` since the last deploy is still deleted (Monaco skips objects that are
already gone). `undeployMonaco` runs `monaco delete` with that file and removes the snapshot;
if the delete fails, the delete file is kept so it can be retried. Every deploy that reached the
tenant — success **or** failure — ends with a closing block naming `undeployMonaco <phase>`, the
delete file and this page, so the way back is the last thing on screen.

**RUM web frontends (`rum-frontends.txt`).** Monaco has no config type for a 3rd-gen RUM
frontend (a "web application"), and a `builtin:rum.web.app-detection` rule needs one that already
exists. A phase folder can declare them, one per line:

```
# monaco/tenant/rum-frontends.txt — <ENV_VAR> <name> <display name>
TODO_RUM_APP_ID todoapp TODOApp
```

Before Monaco runs, `deployMonaco` creates **the seat's own** frontend for each line and exports
`<ENV_VAR>` with its **classic id** (`APPLICATION-…`) — the id a detection rule's
`applicationId` takes; the rule refuses the `FRONTEND-…` id. Use it like any environment
parameter:

```yaml
# monaco/tenant/rum-todoapp/config.yaml
configs:
- id: todoapp-detection
  config:
    name: {type: environment, name: LEARNER_PREFIX}
    parameters:
      applicationId: {type: environment, name: TODO_RUM_APP_ID}
      host: {type: environment, name: TODO_APP_HOST}
    template: rule.json      # {"applicationId": "{{ .applicationId }}", "matcher": "URL_CONTAINS", "pattern": "{{ .host }}", ...}
  type:
    settings: {schema: builtin:rum.web.app-detection, scope: environment}
```

| | Value |
|---|---|
| `frontendName` | `<name>-<seat id>` — seat id is `ORBITAL_JOB_ID`, else `CODESPACE_NAME`, else the hostname, so two sessions of one learner never share a frontend |
| `displayName` | `<display name> <DT_SESSION_ID>`, plus ` (<job>)` under Orbital — e.g. `TODOApp alice-20261020 (k3x9a1b2)` |
| `type` | `WEB_AUTO_INJECTED` (RUM is enabled) |
| state | `.devcontainer/.monaco/<phase>/rum-frontends.state` — `<ENV_VAR>\|<frontendName>\|<FRONTEND id>\|<APPLICATION id>` |

Every Dynatrace API call is printed as `Dynatrace API: <METHOD> <path>` (and the DQL text);
the token never is — it reaches curl through a file descriptor, not its command line.

- **Re-deploy reuses it.** The frontend is looked up by name first
  (`GET …/settings/objects?schemaIds=builtin:rum.frontend.name`) and reused — from the state
  file, else through Smartscape (`smartscapeNodes FRONTEND | filter frontend.name == …`, which
  lags a new frontend by up to ~80 s). It must not be POSTed twice: a second create of the same
  name answers the **same** `FRONTEND` id but adds **another** classic application (measured on
  PVF), and that orphan has no frontend node, so the RUM API cannot delete it.
- **Undo.** `undeployMonaco` runs `monaco delete` first, then
  `DELETE /platform/rum/v1/frontends/<FRONTEND id>` — deleting a frontend also removes the
  detection rules pointing at it — and **proves it gone by reading its
  `builtin:rum.frontend.name` back**. The DELETE's status proves nothing: an id already gone
  answers 204 or 404, and a frontend only seconds old answers 200 or 400 and stays. So it
  deletes and re-reads until the object is gone (~90 s at most); one that is not proven gone
  stays in the state file, `undeployMonaco` returns non-zero, and running it again retries.
- **Nothing leaves the state without evidence.** A line is written *before* the create
  (`<ENV_VAR>|<name>||`, pending), because a create whose answer is lost — timeout, 5xx, no id —
  may still have made the frontend; only an outright refusal (400/401/403) removes it. At undo a
  line missing an id is resolved first (by name, then Smartscape). A failed or incomplete read
  (any non-200, more than 20 pages, a repeating `nextPageKey`) or a lag that outlasts the bound
  keeps the line and fails; a pending line is dropped only after 3 complete listings name no
  such frontend.
- **`<ENV_VAR>` may not be a reserved name** — anything holding a token, key, secret or password,
  `DT_*`, `ORBITAL_*`, `MONACO_*`, `PATH`, `HOME`, `KUBECONFIG`, `LEARNER_PREFIX` and other shell
  or framework variables are refused before any API call.
- **Token.** The seat's `DT_MONACO_TOKEN` carries `rum:frontends:write` and
  `rum:frontends:delete` from app ENH-130 M5. Without them the create answers 403 and
  `deployMonaco` stops before Monaco runs. Needs `jq`.
- **API maturity.** `/platform/rum/v1/frontends` is `EARLY_ADOPTER`; its contract can change.

**Help.** `deployMonaco -h` and `undeployMonaco -h` print the usage, the phases, what each step
does, the required variables (names only) and the undo command, with a link to this section. They
touch nothing — no tenant call, no file.

**Deleted documents stay in the trash.** Monaco deletes a document and then purges it from the
tenant's trash. The learner's token has no `document:trash.documents:delete`, by design (the
token acts as the person whose account owns the tenant's mint client, so a purge scope would
let a learner permanently destroy that person's trashed documents). The purge therefore gets
403 and `monaco delete` exits 1. `undeployMonaco` reads monaco's output: when the **only**
errors are refused trash purges, the undeploy counts as done — the snapshot is removed and it
warns `N document(s) deleted but left in the tenant's trash`. Any other error, mixed in or
alone, still fails and keeps the delete file.

**A trashed document keeps its id.** Monaco derives a document's id from
`<project>:document:<configId>` (`monaco-<uuid>`), and a document in the trash still holds that
id. A redeploy after `undeployMonaco` would create the same id, and the Document API answers
`409 "id already exists: monaco-…"` (measured on PVF, 2026-10-22). The learner token cannot
purge or restore the trash, so the framework moves to fresh ids instead:

- When an undeploy leaves documents in the trash, it bumps a per-phase generation
  (`.devcontainer/.monaco/.<phase>-generation`). The next `deployMonaco <phase>` names the
  project `<repo>-<DT_SESSION_ID>-g<N>`, and with it new document ids. `LEARNER_PREFIX` (and
  so the names a learner sees) does not change.
- A redeploy **without** an undeploy keeps the project, so it updates the same objects in
  place.
- A deploy that still meets the 409 — the generation file is gone because a new container
  started, or an earlier failed undeploy trashed a document — explains the trash, bumps the
  generation, and asks you to run `deployMonaco <phase>` again. The failed attempt's delete
  entries are merged into the next delete file, so `undeployMonaco` still reaches what it left.

**Every command is printed.** `deployMonaco`, `undeployMonaco` and `enableDtctl` print each
`monaco` / `dtctl` command before they run it, as `→ <command>`, so a learner sees the real call
and can run it themselves. Variables are shown by name, never by value:

```
→ cd .devcontainer/.monaco/tenant
→ LEARNER_PREFIX=$DT_SESSION_ID monaco generate deletefile manifest.yaml -o . --file delete.yaml
→ LEARNER_PREFIX=$DT_SESSION_ID monaco deploy manifest.yaml -e learner
→ monaco delete --manifest manifest.yaml --file delete.yaml -e learner
→ dtctl config set-context enablement --environment $DT_ENVIRONMENT --token-ref enablement --safety-level readonly
→ dtctl config set-credentials enablement --token '${DT_DTCTL_TOKEN}'
```

The manifest reads the URL from `$DT_ENVIRONMENT` and the token from `$DT_MONACO_TOKEN` by name.
The RUM frontend API calls are printed as `Dynatrace API: <METHOD> <path>`.

A `configs` file the check cannot read — a flow list (`configs: [...]`) or no top-level
`configs:` — is refused like an unsupported type: the check fails closed. Deploy and
undeploy output lands in the provisioning log through `printInfoSection`.

!!! warning "Nothing deletes tenant config when a container dies"
    `undeployMonaco` runs inside the container. A container that is removed without running
    it leaves its dashboards and settings in the tenant. Removing a learner's config at the end
    of a session is not part of these functions.

---

## Dynatrace Operator & Dynakube

### Deployment

| Function | Signature | Description |
|---|---|---|
| `dynatraceDeployOperator` | `dynatraceDeployOperator [env token ingest]` | Deploys the DT Operator via Helm and creates the credentials secret |
| `deployDynatrace` | `deployDynatrace [mode] [env token ingest]` | Unified deploy: validates credentials, generates Dynakube, waits for pods. No mode = the configured `mode:` (`apponly` by default) |
| `deployCloudNative` | `deployCloudNative [env token ingest]` | Shortcut for `deployDynatrace cloudnative` |
| `deployApplicationMonitoring` | `deployApplicationMonitoring [env token ingest]` | Shortcut for `deployDynatrace apponly` |
| `undeployDynakubes` | `undeployDynakubes` | Deletes all Dynakube CRDs and uninstalls the OneAgent if present |
| `uninstallDynatrace` | `uninstallDynatrace` | Full removal: Dynakubes + Helm release + `dynatrace` namespace |
| `deployOperatorViaHelm` | `deployOperatorViaHelm [args]` | Legacy wrapper for `dynatraceDeployOperator` |
| `undeployOperatorViaHelm` | `undeployOperatorViaHelm` | Uninstalls the `dynatrace-operator` Helm release |

```bash
# Deploy using credentials from env vars, in the configured mode (apponly by default)
deployDynatrace

# Deploy with explicit credentials
deployDynatrace apponly "https://abc.apps.dynatrace.com" "$OP_TOKEN" "$INGEST_TOKEN"

# Available modes: apponly (default), cloudnative, k8s-only
```

### Dynakube Configuration

| Function | Signature | Description |
|---|---|---|
| `loadDynakubeConfig` | `loadDynakubeConfig` | Loads `DK_*` variables from `dynakube-defaults.yaml`, then overlays `dynakube-config.yaml` |
| `generateDynakube` | `generateDynakube [mode]` | Generates `.devcontainer/yaml/gen/dynakube.yaml` from config and current credentials |
| `_parseDynakubeYaml` | `_parseDynakubeYaml <file>` | Internal: parses a flat YAML file into `DK_*` exported variables |
| `getLatestEcrTag` | `getLatestEcrTag <repo>` | Resolves the latest semver tag from ECR public gallery for a DT image |

Config keys (set in `dynakube-config.yaml` or `dynakube-defaults.yaml`) map to `DK_*` variables:

| Key | Variable | Default |
|---|---|---|
| `mode` | `DK_MODE` | `apponly` (`cloudnative`, `k8s-only`) |
| `operator_version` | `DK_OPERATOR_VERSION` | `1.10.2` |
| `dynakube_api_version` | `DK_DYNAKUBE_API_VERSION` | `v1beta6` |
| `log_monitoring` | `DK_LOG_MONITORING` | `true` |
| `telemetry_ingest` | `DK_TELEMETRY_INGEST` | `true` |
| `sensitive_data` | `DK_SENSITIVE_DATA` | `true` |
| `kspm` | `DK_KSPM` | `false` |
| `extensions` | `DK_EXTENSIONS` | `false` |
| `routing` | `DK_ROUTING` | `true` |
| `debugging` | `DK_DEBUGGING` | `false` |
| `dynatrace_api` | `DK_DYNATRACE_API` | `false` |
| `ag_replicas` | `DK_AG_REPLICAS` | `1` |
| `ag_cpu_request` / `ag_cpu_limit` | `DK_AG_CPU_*` | `100m` / `500m` |
| `ag_memory_request` / `ag_memory_limit` | `DK_AG_MEMORY_*` | `512Mi` / `1Gi` |

The authoritative values are [`dynakube-defaults.yaml`](https://github.com/dynatrace-wwse/codespaces-framework/blob/main/.devcontainer/yaml/dynakube-defaults.yaml) — this table mirrors it. How to override them per repo: [DynaKube configuration](dynatrace-integration.md#dynakube-configuration-defaults-and-repo-override).

---

## Kubernetes: Cluster Management

The framework supports two cluster engines: **K3d** (default, lightweight) and **Kind** (for CloudNativeFullStack). Switch with the `CLUSTER_ENGINE` environment variable.

### Unified Cluster API (use these in post-create.sh)

| Function | Description |
|---|---|
| `startCluster` | Starts the cluster based on `CLUSTER_ENGINE` (default: K3d) |
| `stopCluster` | Stops the cluster |
| `deleteCluster` | Deletes the cluster completely |

```bash
# Default: K3d
startCluster

# Switch to Kind for CloudNativeFullStack
export CLUSTER_ENGINE=kind
startCluster
```

### K3d Cluster (default engine)

K3d (K3s in Docker) is the default cluster engine — 3× faster startup and lower resource usage than Kind.

| Function | Description |
|---|---|
| `startK3dCluster` | Starts, creates, or attaches to a K3d cluster named `enablement` |
| `createK3dCluster` | Creates a new K3d cluster (configurable via env vars) |
| `attachK3dCluster` | Merges kubeconfig and switches context to the K3d cluster |
| `stopK3dCluster` | Stops the K3d cluster |
| `deleteK3dCluster` | Deletes the K3d cluster |
| `installK3d` | Installs k3d CLI if not already present |

K3d port configuration (env vars for `createK3dCluster`):

| Variable | Default | Purpose |
|---|---|---|
| `K3D_CLUSTER_NAME` | `enablement` | Cluster name |
| `K3D_LB_HTTP_PORT` | `80` | Host port for ingress port 80 |
| `K3D_LB_HTTPS_PORT` | `443` | Host port for ingress port 443 |
| `K3D_API_PORT` | `6443` | Kubernetes API port |

```bash
# On ops-server where nginx owns 80/443:
export K3D_LB_HTTP_PORT=30080 K3D_LB_HTTPS_PORT=30443 K3D_API_PORT=6444
startK3dCluster
```

Backward-compatibility aliases: `startK3sCluster`, `createK3sCluster`, `attachK3sCluster`, `stopK3sCluster`, `deleteK3sCluster`.

### Kind Cluster

Kind (Kubernetes IN Docker) is used when CloudNativeFullStack is required — OneAgent DaemonSet needs real Linux nodes, not Docker containers.

| Function | Description |
|---|---|
| `startKindCluster` | Starts, creates, or attaches to an existing Kind cluster; installs ingress controller |
| `createKindCluster` | Creates a new Kind cluster using the framework's kind-cluster.yml |
| `attachKindCluster` | Writes the kubeconfig for a running Kind cluster to `~/.kube/config` |
| `stopKindCluster` | Stops the `kind-control-plane` Docker container |
| `deleteKindCluster` | Deletes the Kind cluster completely |

```bash
# K3d is the default — use Kind only for CNFS
export CLUSTER_ENGINE=kind
startKindCluster    # safe to call even if cluster already exists
deleteKindCluster   # destructive, removes the cluster
```

!!! warning "CloudNativeFullStack on K3d"
    OneAgent DaemonSet will CrashLoopBackOff on K3d — its nodes are Docker containers without real Linux host mounts. Use `deployApplicationMonitoring` (AppOnly mode) on K3d. Use `deployCloudNative` only with Kind.

### Pod Waiting

| Function | Signature | Description |
|---|---|---|
| `waitForPod` | `waitForPod [namespace] <pod-name-pattern>` | Waits until at least one matching pod is scheduled (max 10 min) |
| `waitForAllPods` | `waitForAllPods [namespace]` | Waits until all pods are `Running` or `Completed`; every 2 minutes, and before it fails, prints `printPodsDiagnostics` |
| `printPodsDiagnostics` | `printPodsDiagnostics [-n namespace]` | Why pods are not running: host disk and memory, node pressure and allocation, each stuck pod's reason and exit code, Warning events. Read-only |
| `waitForAllReadyPods` | `waitForAllReadyPods [namespace]` | Waits until all pods are fully `Ready` (all containers up) |
| `waitAppCanHandleRequests` | `waitAppCanHandleRequests [port] [retries]` | Polls `http://localhost:<port>` until it returns HTTP 200 |

```bash
waitForPod dynatrace activegate
waitForAllPods dynatrace
waitForAllReadyPods hipstershop
waitAppCanHandleRequests 8080 20   # port 8080, 20 retries
```

All waiting functions retry every 10 seconds for up to 60 attempts (10 minutes), then exit with error.

---

## Ingress & App Exposure

The framework routes apps via nginx ingress with magic DNS (`sslip.io`), eliminating the need for manual NodePort assignments.

| Function | Signature | Description |
|---|---|---|
| `installIngressController` | `installIngressController` | Installs nginx ingress controller (auto-selects K3d `cloud` or Kind `kind` provider) |
| `detectIP` | `detectIP` | Returns the host IP used for DNS subdomains (auto-detects: Codespaces/public/local) |
| `detectHostname` | `detectHostname` | Returns the machine hostname used as a secondary ingress host |
| `getAppURL` | `getAppURL <app-name> [port]` | Returns the user-facing URL for an app (Codespaces vs. sslip.io) |
| `registerApp` | `registerApp <name> <ns> <svc> <port> [annotations]` | Creates an Ingress and registers the app in the app registry |
| `unregisterApp` | `unregisterApp <app-name> <namespace>` | Deletes the Ingress and removes the app from the registry |
| `listApps` | `listApps` | Lists all registered apps with their accessible URLs |
| `registerMkdocs` | `registerMkdocs` | Registers the mkdocs dev server (port 8000) through the ingress |
| `registerAstroshopIngress` | `registerAstroshopIngress [namespace]` | Creates a multi-path ingress for Astroshop (OTel routes + frontend) |
| `registerOpentelemetryDemoIngress` | `registerOpentelemetryDemoIngress [namespace]` | Creates a multi-path ingress for the CNCF OTel Demo |

```bash
# Register a custom app
registerApp "myapp" "myapp-ns" "myapp-svc" 8080

# List all running apps
listApps

# Remove an app
unregisterApp "myapp" "myapp-ns"
```

---

## App Deployment

### Interactive Deployer

```bash
deployApp           # prints usage with all available apps
deployApp astroshop # deploy by name
deployApp 2         # deploy by number
deployApp 2 -d      # undeploy by number
```

Available apps:

| # | Key | App | AMD | ARM |
|---|---|---|---|---|
| 1 | a | ai-travel-advisor | ✅ | ✅ |
| 2 | b | astroshop | ✅ | ❌ |
| 3 | c | bugzapper | ✅ | ✅ |
| 4 | d | easytrade | ✅ | ❌ |
| 5 | e | hipstershop | ✅ | ❌ |
| 6 | f | todoapp | ✅ | ✅ |
| 7 | g | unguard | ✅ | ❌ |
| 8 | h | opentelemetry-demo | ✅ | ✅ |
| 9 | i | gitlab (GitLab CE + Runner) | ✅ | ✅ |

### Individual Deploy Functions

| Function | Description |
|---|---|
| `deployAITravelAdvisorApp` | Deploys AI Travel Advisor with Ollama LLM and Weaviate vector DB; requires `DT_LLM_TOKEN` |
| `deployTodoApp` | Deploys a simple Node.js Todo app |
| `deployAstroshop` | Deploys the Dynatrace-curated Astroshop; requires `DT_INGEST_TOKEN` |
| `deployBugZapperApp` | Deploys the BugZapper browser game |
| `deployEasyTrade` | Deploys the EasyTrade demo application |
| `deployHipsterShop` | Deploys the HipsterShop microservices demo |
| `deployUnguard` | Deploys Unguard (intentionally vulnerable app, for security demos) |
| `undeployUnguard` | Removes Unguard and its MariaDB |
| `deployOpentelemetryDemo` | Deploys the CNCF OpenTelemetry Demo (upstream, community-maintained) |
| `undeployOpentelemetryDemo` | Removes the OTel Demo and its namespace |
| `deployGitlab` / `uninstallGitlab` | GitLab CE (Omnibus, one Deployment) + Runner — see [GitLab & browser IDE](gitlab-ide.md) |
| `startCodeServer` / `stopCodeServer` | VS Code in the browser inside the learner container — see [GitLab & browser IDE](gitlab-ide.md) |

All deploy functions expose apps exclusively via nginx ingress (`registerApp`). The legacy NodePort path has been removed.

---

## Lab Step Checks & Solution Helpers

Checks for a lesson's `shell-verification` blocks and helpers for its `LAB_SOLUTION` commands. They used to live in each repo's `my_functions.sh` (enablement-kubernetes-101, enablement-app-training-template); since 1.12.0 every repo gets them from the framework.

```yaml
command: "source .devcontainer/util/source_framework.sh >/dev/null 2>&1 && checkNodeReady"
expect:
  operator: exit-zero
```

| Function | Signature | Passes when |
|---|---|---|
| `checkNodeReady` | `checkNodeReady` | a cluster node is `Ready` |
| `checkTodoAppRunning` | `checkTodoAppRunning` | a pod in `todoapp` is `Running` |
| `checkOperatorReady` | `checkOperatorReady` | the Dynatrace Operator pod is `Running` |
| `checkDynakube` | `checkDynakube` | a DynaKube exists in `dynatrace` |
| `checkActiveGateReady` | `checkActiveGateReady` | the ActiveGate pod is `Running` |
| `checkLogModuleReady` | `checkLogModuleReady` | the log monitoring pod is `Running` |
| `checkOneAgentInjected` | `checkOneAgentInjected [namespace]` | a pod in the namespace (default `todoapp`) carries `oneagent.dynatrace.com/injected: "true"` |
| `restartTodoApp` | `restartTodoApp` | restarts the todoapp deployment and waits for the rollout (max 180 s) |
| `generateTodoTraffic` | `generateTodoTraffic [title]` | creates a TODO through the app's HTTP API, so logs and a `POST /todos` trace reach Grail |

**Click vs. automation.** A learner's click probes once and answers at once: `0` passes, `1` fails with a hint. Automation (the training test, resume, Run solution, the editor's Test step) exports `LAB_WAIT=1`, and the check then waits for the expected state before probing. A wait that times out ends the wait, never the shell: the probe after it gives the verdict. Each check also has a `waitFor…` wrapper (`waitForNodeReady`, `waitForOneAgentInjected`, …) that is the check with `LAB_WAIT=1`.

**`generateTodoTraffic` waits for the endpoint.** A finished `kubectl rollout status` only means the container started; the app answers HTTP some seconds later. The helper therefore retries the endpoint before it posts: up to 60 s, 150 s with `LAB_WAIT=1`, or `TODO_TRAFFIC_WAIT` seconds when set. It fails with a clear message if the endpoint never answers. `restartTodoApp && generateTodoTraffic` is safe in one solution command. Match the app's log line `Adding a new todo` in a DQL check, not the title.

A repo's `my_functions.sh` is sourced after `functions.sh`, so a repo that still defines one of these functions keeps its own copy until it deletes it.

---

## Cert-Manager

| Function | Description |
|---|---|
| `certmanagerInstall` | Installs cert-manager from the Jetstack manifest |
| `certmanagerDelete` | Removes cert-manager |
| `certmanagerEnable` | Creates a Let's Encrypt `ClusterIssuer` (generates a random email if `CERTMANAGER_EMAIL` is not set) |
| `deployCertmanager` | Convenience wrapper: installs + enables cert-manager in one step |
| `generateRandomEmail` | Returns a random `email-XXXX@dynatrace.ai` string |

```bash
deployCertmanager
# or step by step:
certmanagerInstall
CERTMANAGER_EMAIL="myemail@example.com" certmanagerEnable
```

---

## Tool Installation

| Function | Description |
|---|---|
| `installHelm` | Installs Helm and adds the Bitnami chart repository |
| `installHelmDashboard` | Installs and starts the Helm Dashboard plugin (port 8002) |
| `installKubernetesDashboard` | Installs the Kubernetes Dashboard via Helm and port-forwards on 8001 |
| `installK9s` | Installs the k9s CLI |
| `installRunme` | Installs the Runme CLI (architecture-aware: AMD/ARM) |
| `installMonaco` | Installs Monaco, checksum-verified — see [dtctl & Monaco](#dtctl-monaco-opt-in) |
| `enableDtctl` | Installs and configures dtctl, checksum-verified — see [dtctl & Monaco](#dtctl-monaco-opt-in) |

---

## MkDocs & Documentation

| Function | Description |
|---|---|
| `installMkdocs` | Installs Runme + MkDocs from requirements file, fetches base config, starts server |
| `fetchMkdocsBase` | For an `INHERIT` repo, downloads `mkdocs-base.yaml` and `docs/stylesheets/extra.css` from the framework at `FRAMEWORK_VERSION` — each only if missing and not committed; a failed download warns and returns 0 |
| `exposeMkdocs` | Starts `mkdocs serve` in the background and registers it via ingress (or prints URL) |
| `registerMkdocs` | Creates a K8s Service + Endpoints + Ingress to proxy the host's mkdocs port 8000 |
| `deployGhdocs` | Deploys the docs to GitHub Pages via `mkdocs gh-deploy` |
| `calculateReadingTime` | Estimates reading time for all `.md` files in the `docs/` directory |

---

## Codespace Lifecycle

| Function | Description |
|---|---|
| `finalizePostCreation` | Entry point called at the end of `post-create.sh`: runs e2e tests if `CODESPACE_NAME` starts with `dttest-`, otherwise verifies creation and sends telemetry |
| `postCodespaceTracker` | Sends a DT biz event with codespace creation metrics (repo, duration, errors, type, architecture) |
| `verifyContainerCreation` | Parses container/codespace logs for errors, populates `ERROR_COUNT` and `CODESPACE_ERRORS` |
| `calculateTime` | Records and exports the codespace creation duration in seconds |
| `updateEnvVariable` | Updates a variable's value in the count file (handles both bash and zsh indirect expansion) |
| `deleteCodespace` | Deletes the current codespace via `gh codespace delete` |
| `deleteCache` | Removes both the container cache (`.devcontainer/.cache`) and host cache (`~/.cache/dt-framework`) |
| `runIntegrationTests` | Runs the repo's integration test suite |

---

## Host & System Utilities

| Function | Signature | Description |
|---|---|---|
| `checkHost` | `checkHost` | Verifies that `make`, `docker`, `node`, and `npm` are installed and accessible; offers to auto-install missing tools |
| `freeUpSpace` | `freeUpSpace` | Cleans APT, pip, npm/yarn/pnpm caches, Docker images/volumes, and old temp files |
| `showOpenPorts` | `showOpenPorts` | Shows all open TCP/UDP listening ports via `netstat` |
| `getRunningDockerContainernameByImagePattern` | `getRunningDockerContainernameByImagePattern <pattern>` | Returns the name of a running Docker container whose image matches the pattern |

```bash
checkHost       # verify and optionally fix host prerequisites
freeUpSpace     # reclaim disk space (safe to run at any time)
showOpenPorts   # list all listening ports
```
