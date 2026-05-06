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
| `bindFunctionsInShell` | Appends the `source_framework.sh` loader and greeting call to `.zshrc` |
| `setupAliases` | Adds shell and `kubectl` convenience aliases to `.zshrc` |

`setUpTerminal` is called once during container creation. Aliases added by `setupAliases`:

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

---

## Dynatrace Operator & Dynakube

### Deployment

| Function | Signature | Description |
|---|---|---|
| `dynatraceDeployOperator` | `dynatraceDeployOperator [env token ingest]` | Deploys the DT Operator via Helm and creates the credentials secret |
| `deployDynatrace` | `deployDynatrace [mode] [env token ingest]` | Unified deploy: validates credentials, generates Dynakube, waits for pods |
| `deployCloudNative` | `deployCloudNative [env token ingest]` | Shortcut for `deployDynatrace cloudnative` |
| `deployApplicationMonitoring` | `deployApplicationMonitoring [env token ingest]` | Shortcut for `deployDynatrace apponly` |
| `undeployDynakubes` | `undeployDynakubes` | Deletes all Dynakube CRDs and uninstalls the OneAgent if present |
| `uninstallDynatrace` | `uninstallDynatrace` | Full removal: Dynakubes + Helm release + `dynatrace` namespace |
| `deployOperatorViaHelm` | `deployOperatorViaHelm [args]` | Legacy wrapper for `dynatraceDeployOperator` |
| `undeployOperatorViaHelm` | `undeployOperatorViaHelm` | Uninstalls the `dynatrace-operator` Helm release |

```bash
# Deploy using credentials from env vars
deployCloudNative

# Deploy with explicit credentials
deployDynatrace apponly "https://abc.apps.dynatrace.com" "$OP_TOKEN" "$INGEST_TOKEN"

# Available modes: cloudnative (default), apponly, k8s-only
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
| `mode` | `DK_MODE` | `cloudnative` |
| `operator_version` | `DK_OPERATOR_VERSION` | `1.9.0` |
| `ag_replicas` | `DK_AG_REPLICAS` | `1` |
| `log_monitoring` | `DK_LOG_MONITORING` | `true` |
| `kspm` | `DK_KSPM` | `false` |
| `telemetry_ingest` | `DK_TELEMETRY_INGEST` | `false` |
| `extensions` | `DK_EXTENSIONS` | `false` |
| `sensitive_data` | `DK_SENSITIVE_DATA` | `false` |

---

## Kubernetes & Kind

### Pod Waiting

| Function | Signature | Description |
|---|---|---|
| `waitForPod` | `waitForPod [namespace] <pod-name-pattern>` | Waits until at least one matching pod is scheduled (max 10 min) |
| `waitForAllPods` | `waitForAllPods [namespace]` | Waits until all pods are `Running` or `Completed` |
| `waitForAllReadyPods` | `waitForAllReadyPods [namespace]` | Waits until all pods are fully `Ready` (all containers up) |
| `waitAppCanHandleRequests` | `waitAppCanHandleRequests [port] [retries]` | Polls `http://localhost:<port>` until it returns HTTP 200 |

```bash
waitForPod dynatrace activegate
waitForAllPods dynatrace
waitForAllReadyPods hipstershop
waitAppCanHandleRequests 8080 20   # port 8080, 20 retries
```

All waiting functions retry every 10 seconds for up to 60 attempts (10 minutes), then exit with error.

### Kind Cluster

| Function | Description |
|---|---|
| `startKindCluster` | Starts, creates, or attaches to an existing Kind cluster; installs ingress controller |
| `createKindCluster` | Creates a new Kind cluster using the framework's kind-cluster.yml |
| `attachKindCluster` | Writes the kubeconfig for a running Kind cluster to `~/.kube/config` |
| `stopKindCluster` | Stops the `kind-control-plane` Docker container |
| `deleteKindCluster` | Deletes the Kind cluster completely |

```bash
startKindCluster    # safe to call even if cluster already exists
stopKindCluster     # preserves the container, restartable
deleteKindCluster   # destructive, removes the cluster
```

---

## Ingress & App Exposure

The framework routes apps via nginx ingress with magic DNS (`sslip.io`), eliminating the need for manual NodePort assignments.

| Function | Signature | Description |
|---|---|---|
| `installIngressController` | `installIngressController` | Installs nginx ingress controller in the Kind cluster |
| `detectIP` | `detectIP` | Returns the host IP used for DNS subdomains (auto-detects: Codespaces/public/local) |
| `getAppURL` | `getAppURL <app-name> [port]` | Returns the user-facing URL for an app (Codespaces vs. sslip.io) |
| `registerApp` | `registerApp <name> <ns> <svc> <port> [annotations]` | Creates an Ingress, registers in the app registry, sets up Codespaces port-forward |
| `unregisterApp` | `unregisterApp <app-name> <namespace>` | Deletes the Ingress, removes port-forward, removes from registry |
| `listApps` | `listApps` | Lists all registered apps with their accessible URLs |
| `getNextCodespacesPort` | `getNextCodespacesPort` | Returns the next unused port starting at `INGRESS_CS_PORT_START` |
| `getNextFreeAppPort` | `getNextFreeAppPort` | Returns the next free NodePort from `NODE_PORTS` (legacy mode only) |
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

All deploy functions handle both ingress mode (default) and legacy NodePort mode (`USE_LEGACY_PORTS=true`).

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

---

## MkDocs & Documentation

| Function | Description |
|---|---|
| `installMkdocs` | Installs Runme + MkDocs from requirements file, fetches base config, starts server |
| `fetchMkdocsBase` | Downloads `mkdocs-base.yaml` from the framework if it is missing locally |
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
| `verifyCodespaceCreation` | Parses container/codespace logs for errors, populates `ERROR_COUNT` and `CODESPACE_ERRORS` |
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
