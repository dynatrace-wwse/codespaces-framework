

!!! example "Quality Assurance"
    To maintain high standards across all repositories using the enablement framework, a robust testing strategy is enforced. This ensures every repository remains reliable, consistent, and production-ready.

## 🧪 Integration Testing on Pull Requests

- **Automated Integration Tests:**  
  Every repository must include integration tests that run automatically on every Pull Request (PR). This ensures new changes do not break existing functionality.

- **integration.sh Script:**  
  The core of the testing process is the `integration.sh` script, located at `.devcontainer/test/integration.sh` in each repository. This script is adapted per repo and is triggered by a GitHub Actions workflow on every PR.
  - The workflow provisions a full environment (K3d cluster + Dynatrace Operator + demo apps).
  - Once the environment is ready, `integration.sh` runs a series of assertions to verify that pods, services, and applications are running as expected.

- **On-Demand Testing:**  
  Run integration tests locally using the Makefile:
  ```bash
  cd .devcontainer && make integration
  ```

### Example: integration.sh

```bash title=".devcontainer/test/integration.sh" linenums="1"
#!/bin/bash
# Load framework
source .devcontainer/util/source_framework.sh

printInfoSection "Running integration Tests for $RepositoryName"

# Kubernetes cluster health
assertRunningPod kube-system coredns

# Dynatrace Operator components
assertRunningPod dynatrace operator
assertRunningPod dynatrace activegate

# Demo application
assertRunningPod todoapp todoapp

# App is reachable via nginx ingress + magic DNS (sslip.io)
assertRunningApp todoapp

printInfoSection "Integration tests completed for $RepositoryName"
```

---

## 🔬 Test Functions Reference

All test assertion functions are defined in `.devcontainer/test/test_functions.sh` and are automatically loaded into every shell session via `functions.sh`.

### Pod & Container Assertions

| Function | Signature | Description |
|----------|-----------|-------------|
| `assertRunningPod` | `assertRunningPod <namespace> <name>` | Verifies pods matching `name` exist and are running in `namespace`. Exits 1 if none found. |
| `assertRunningContainer` | `assertRunningContainer <name>` | Verifies a Docker container with `name` is running (via `docker ps`). |

```bash
assertRunningPod dynatrace operator        # checks DT operator pods
assertRunningPod kube-system coredns       # checks CoreDNS
assertRunningContainer my-sidecar          # checks docker container
```

### HTTP & Application Assertions

| Function | Signature | Description |
|----------|-----------|-------------|
| `assertRunningApp` | `assertRunningApp <app-name>` | Probes the app via nginx ingress using both magic-DNS (`app.<ip>.sslip.io`) and hostname-based hosts. Retries up to 8× with 3s spacing. |
| `assertRunningHttp` | `assertRunningHttp <port> [path]` | Asserts an HTTP endpoint on localhost returns 200 OK. Retries up to 5× with 3s delay. |

```bash
assertRunningApp todoapp            # ingress-based check (K3d + sslip.io)
assertRunningHttp 8000 /health      # direct port check (MkDocs, etc.)
```

!!! note "assertRunningApp vs assertRunningHttp"
    `assertRunningApp` is the modern check — it validates that nginx ingress routing is working correctly with magic-DNS. Use `assertRunningHttp` only for services exposed directly on a host port (not through ingress).

### Ingress & Deployment Assertions

| Function | Signature | Description |
|----------|-----------|-------------|
| `assertIngressRoute` | `assertIngressRoute <app-name> <namespace>` | Verifies an Ingress resource named `<app-name>-ingress` exists in the namespace and has a host rule. |
| `assertAppDeployed` | `assertAppDeployed <app-name> <namespace> [port]` | Full stack check: pod running + ingress route (or NodePort if USE_LEGACY_PORTS=true). |

```bash
assertIngressRoute todoapp todoapp        # checks Ingress resource
assertAppDeployed astroshop astroshop    # pod + ingress
```

### Environment Variable Assertions

| Function | Signature | Description |
|----------|-----------|-------------|
| `assertEnvVariable` | `assertEnvVariable <var-name> [pattern]` | Asserts an env variable is set and optionally matches a regex pattern. |

```bash
assertEnvVariable DT_ENVIRONMENT
assertEnvVariable DT_ENVIRONMENT "^https://.*\.dynatrace\.com"
assertEnvVariable FRAMEWORK_VERSION "^1\."
```

---

## 🧩 Unit Tests (BATS)

The framework includes a suite of shell unit tests using [BATS (Bash Automated Testing System)](https://bats-core.readthedocs.io/). Unit tests do not require Docker or Kubernetes — they test shell function logic in isolation.

```bash
# Run unit tests on the host
cd .devcontainer && make test

# Run unit tests inside the running container
cd .devcontainer && make test-in-container
```

Unit tests live in `.devcontainer/test/unit/`. The framework ships with 78+ unit tests covering:
- `variablesNeeded` validation logic
- `parseDynatraceEnvironment` URL parsing and export
- Token format validation
- Cluster engine routing
- Port allocation logic

---

## 🚀 Framework CI Test Suites

Beyond per-repo integration tests, the framework maintains its **own CI test suite** — a set of end-to-end tests that validate the framework itself across different cluster engines, app deployments, and Dynatrace monitoring modes. Tests run inside Sysbox containers on the [Orbital ops platform](ops-platform.md), on both AMD64 and ARM64 workers.

### Test suite catalog

| Suite ID | Name | Arch | Description | Requires credentials |
|---|---|---|---|---|
| `bats` | Unit Tests | ARM64 | Shell unit tests — no cluster needed | No |
| `engines` | Engine Tests | AMD64 + ARM64 | K3d + Kind dual-engine ingress validation (Kind skipped on Orbital/Sysbox) | No |
| `k3d-apps` | K3d App Exposure | AMD64 + ARM64 | All demo apps deployed and exposed via ingress on K3d | No |
| `dt-apponly` | DT Application Monitoring | AMD64 + ARM64 | Full Dynatrace operator + ActiveGate + CSI code injection + todo-app on K3d | **Yes** |
| `dt-cnfs` | DT CloudNative FullStack | AMD64 + ARM64 | CNFS dynakube + K3d — validates operator, ActiveGate, dynakube spec, app | **Yes** |

Credentialed tests (`dt-apponly`, `dt-cnfs`) run against the **COE tenant** (`geu80787.apps.dynatrace.com`). Credentials are injected per-job from the ops server's environment.

---

### DT Application Monitoring test (`dt-apponly`)

**File:** `.devcontainer/test/integration_appmon_k3d_todoapp.sh`

Tests Dynatrace `ApplicationMonitoring` (apponly) mode end-to-end on a fresh K3d cluster:

1. Credential pre-check (`DT_ENVIRONMENT`, `DT_OPERATOR_TOKEN`, `DT_INGEST_TOKEN`)
2. Start K3d cluster
3. Deploy Dynatrace Operator (`dynatraceDeployOperator`)
4. Deploy ApplicationMonitoring (`deployApplicationMonitoring`)
5. Deploy todo-app (`deployTodoApp`)
6. Assert operator, activegate, todo-app pods running
7. Assert todo-app reachable via ingress (`assertRunningApp`)
8. Assert dynakube YAML contains `applicationMonitoring:` spec
9. Delete cluster

Runs on **both AMD64 and AMD workers** — the DT code module (CSI init container) differs per CPU architecture; this test validates the correct image is pulled and injected on each.

---

### DT CloudNative FullStack test (`dt-cnfs`)

**File:** `.devcontainer/test/integration_cnfs_k3d_todoapp.sh`

Tests Dynatrace `CloudNativeFullStack` mode on K3d. Same sequence as `dt-apponly` but uses `deployCloudNative`.

!!! warning "Known limitation — OneAgent DaemonSet on K3d"
    K3d nodes are Docker containers. OneAgent's host init module requires access to real kernel interfaces (`/proc`, `/sys`) unavailable inside container nodes. The DaemonSet will be in **CrashLoopBackOff** — this is expected and **the test passes despite it**.

    On Orbital/Sysbox, Sysbox adds an additional restriction on host-level syscalls.

    **What IS validated:** Operator running, ActiveGate running, dynakube `cloudNativeFullStack:` spec, todo-app deployed and reachable.  
    **What is NOT validated:** OneAgent DaemonSet running state.

    **Future:** Full OneAgent validation requires real VM nodes. Tracked for when training environments migrate off Sysbox containers to bare-metal VMs.

---

### Triggering suites

=== "Orbital Dashboard"

    Navigate to `https://autonomous-enablements.whydevslovedynatrace.com` → **Framework** tab.  
    Select a suite and click **Trigger**. Results stream live in the log viewer.

=== "API"

    ```bash
    # Single suite — AMD64
    curl -s -X POST https://autonomous-enablements.whydevslovedynatrace.com/api/framework/trigger \
      -H "Content-Type: application/json" \
      -d '{"suite": "dt-apponly", "ref": "main", "arch": "amd64"}'

    # Single suite — ARM64
    curl -s -X POST ... -d '{"suite": "dt-apponly", "ref": "main", "arch": "arm64"}'

    # All suites (both arches)
    curl -s -X POST ... -d '{"suite": "all", "ref": "main"}'
    ```

=== "Local (in container)"

    ```bash
    # Requires .devcontainer/.env with DT credentials
    bash .devcontainer/test/integration_appmon_k3d_todoapp.sh
    bash .devcontainer/test/integration_cnfs_k3d_todoapp.sh
    ```

### Checking last results

Last-run result per suite is stored in Redis and exposed via the API:

```bash
# All suites with last-run status
curl https://autonomous-enablements.whydevslovedynatrace.com/api/framework/suites | jq .

# Recent runs (last 20)
curl https://autonomous-enablements.whydevslovedynatrace.com/api/framework/runs | jq .
```

Or directly via Redis:
```bash
redis-cli hgetall framework:suite:dt-apponly:last
redis-cli hgetall framework:suite:dt-cnfs:last
```

---

## 🌙 Nightly Builds

The framework runs a **nightly scheduled job** that tests every enabled repository and the framework's own CI suites. This catches regressions introduced by upstream changes (new DT operator versions, K3d releases, base image updates) even without a PR.

### What runs nightly

| Target | Queue | Suites |
|---|---|---|
| All 27 enabled repos | `queue:test:arm64` + `queue:test:amd64` | Per-repo `integration.sh` |
| Framework: unit tests | `queue:test:arm64` | `bats` |
| Framework: engine + app tests | `queue:test:arm64` + `queue:test:amd64` | `engines`, `k3d-apps` |
| Framework: DT tests | `queue:test:arm64` + `queue:test:amd64` | `dt-apponly`, `dt-cnfs` |

Jobs are staggered to avoid saturating worker capacity simultaneously.

### Nightly schedule

The nightly scheduler runs via systemd timer on the ops server:

```
ops-nightly.timer  →  ops-nightly.service  →  scheduler.py
```

Check the next scheduled run:
```bash
sudo systemctl list-timers ops-nightly
sudo journalctl -u ops-nightly --since "24 hours ago" --no-pager | tail -20
```

### Manual trigger (with framework tests)

```bash
# On the ops server
cd /home/ops/enablement-framework/codespaces-framework/ops-server/nightly
sudo -u ops python3 scheduler.py --include-framework

# Dry run (shows what would be queued, no actual jobs)
sudo -u ops python3 scheduler.py --include-framework --dry-run

# Single repo only
sudo -u ops python3 scheduler.py --repo codespaces-framework
```

### Monitoring nightly results

All nightly runs are tagged with a `nightly_run_id` and visible in:

- **Orbital dashboard** → Builds tab → filter by trigger `nightly`
- **Redis:** `LRANGE jobs:completed 0 50` (capped at 500 entries)
- **Framework suites:** `/api/framework/runs` (last 20 framework suite results)
- **COE DT tenant** → CI/CD & Orbital dashboard — BizEvents with `trigger=nightly`

---

## Git Strategy

!!! example "Git Strategy & GitHub Actions Workflow"
    ![run codespace](img/git_strategy.png){ align=center ; width="800";}

## 🔒 Branch Protection

**Main Branch Protection:**  
  The `main` branch is protected and will only accept PRs that pass all integration tests. This ensures only thoroughly tested code is merged, maintaining the integrity of the repository.

---


## 🛡️ Integration Test Badges

All repositories in the enablement framework display an **integration test badge** to show the current status of their automated tests. This badge provides immediate visibility into the health of each repository.

For example, the badge for this repository is:

![Integration tests](https://github.com/dynatrace-wwse/codespaces-framework/actions/workflows/integration-tests.yaml/badge.svg){ align=center;  }

You can find a table with all enablement framework repositories and their current integration test status in the [README section of this repository](https://github.com/dynatrace-wwse/codespaces-framework).

---

By following these standards, the enablement framework enforces continuous quality assurance and reliability across all managed repositories.


<div class="grid cards" markdown>
- [Continue to Monitoring →](monitoring.md)
</div>
