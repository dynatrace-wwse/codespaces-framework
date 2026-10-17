# GitLab & browser IDE (developer sessions)

Two framework building blocks for developer-style trainings: a **GitLab** with a CI **Runner**
inside the learner's Kubernetes cluster, and **VS Code in the browser** (code-server) running in
the learner's container. Both are registered like every other app (`registerApp`), so Orbital lists
them with the workspace's apps.

| Function | What it does |
|---|---|
| `deployGitlab` | GitLab CE + Runner in namespace `gitlab`, registered as app `gitlab` (also `deployApp gitlab` / `deployApp 9`) |
| `waitForGitlabReady [seconds]` | Waits until GitLab's sign-in page answers (readiness probe) |
| `checkGitlabRunning` / `waitForGitlabRunning` | Lab-step check: the GitLab pod is ready |
| `gitlabCredentials` | Prints the user (`root`) and the **command** that reveals the password |
| `gitlabSeedRepo <name> <dir>` | Creates project `root/<name>` if missing and pushes `<dir>`'s files to `main` |
| `uninstallGitlab` | Removes the Runner, the ingress + registry row, and the namespace (data included) |
| `installCodeServer [version]` | Installs code-server from its release tarball, checksum-verified |
| `startCodeServer [dir]` | Starts code-server on `CODE_SERVER_PORT` (8443), opening `dir` (default: the repo) |
| `stopCodeServer` | Ends code-server and every extension host and terminal it started |
| `registerCodeServer` | Registers code-server as app `ide` (run by `startCodeServer` under Orbital) |
| `checkCodeServerRunning` | `/healthz` answers 200 |
| `uninstallCodeServer` | Stops it, removes the registration and the install (settings stay) |

## GitLab

### What is deployed

* **GitLab Omnibus CE** (`GITLAB_VERSION`, default `18.4.0-ce.0`) as **one Deployment** with one
  PVC (`gitlab-data`: config, data and logs) and one Service (`gitlab:80`). Omnibus, not the
  cloud-native Helm chart: measured on the same host it is ~1.5 GiB lighter (2.3 vs 3.85 GiB) and
  starts as fast.
* GitLab's own *memory-constrained* settings: Puma in single mode (`worker_processes = 0`), Sidekiq
  concurrency 10, and no Prometheus, container registry, KAS, Pages or Mattermost. Git over
  **HTTPS only** (only the HTTP port of a learner environment is published).
* **GitLab Runner** from its own chart (`GITLAB_RUNNER_CHART_VERSION`, default `0.81.0` = runner
  18.4), **Kubernetes executor**, jobs run as pods in namespace `gitlab` (default image
  `alpine:3.20`). It is registered automatically as an instance runner that runs untagged jobs.
  A training whose pipeline deploys into other namespaces grants that RBAC itself.

### Address

GitLab's `external_url` is the address the learner sees, so clone links, redirects and e-mails are
right:

| Where | External URL |
|---|---|
| Orbital | `https://gitlab--<job>.<ORBITAL_APP_DOMAIN>` (TLS ends at Orbital) |
| Codespaces | the forwarded port-80 URL |
| elsewhere | `http://gitlab.<ip>.sslip.io` |

`GITLAB_EXTERNAL_URL` overrides it. `ORBITAL_APP_DOMAIN` follows `ORBITAL_BASE_URL`, so an
environment started by a staging Orbital gets `…staging.autonomous-enablements…` addresses; this
also applies to every other app's `getAppURL`.

For an **https** external URL, Omnibus' nginx sets `X-Forwarded-Proto: https` towards Rails itself:
Rails only issues its `Secure` session cookie for a request it believes was https, and this way no
proxy in front of the cluster has to forward the scheme for sign-in to work.

The ingress (created by `registerApp gitlab gitlab gitlab 80`) answers on the app's sslip host,
the hostname host and the catch-all, with `proxy-body-size: 512m` and request buffering off, so
large `git push`es pass the cluster. (A proxy in front, such as Orbital's nginx, needs its own
body-size limit.)

### Credentials — never printed

* **Root password**: generated on first deploy straight into secret `gitlab/gitlab-root` (through a
  pipe, never on a command line) and handed to GitLab as an env reference. Nothing prints it: setup
  output is stored by Orbital. To sign in, run the command `gitlabCredentials` shows:

    ```bash
    kubectl -n gitlab get secret gitlab-root -o jsonpath='{.data.password}' | base64 -d; echo
    ```

* **API token** (PAT, scopes `api`, `read_repository`, `write_repository`): minted in-cluster with
  `gitlab-rails runner` into secret `gitlab/gitlab-pat`. API calls run inside the GitLab pod with
  the token on **stdin** (`curl -H @-`).
* **Runner token**: created through the API into secret `gitlab/gitlab-runner-secret`.
* `gitlabSeedRepo` gives git the token through `GIT_ASKPASS` — never in a URL or on a command line,
  so it is not in `ps`, in `.git/config` or in shell history.

### Idempotent

Re-running `deployGitlab` keeps the password, the data, a token that still authenticates and a
runner GitLab still knows; it replaces a token GitLab rejects. GitLab's pod rolls only when its
configuration changed (for example a new external URL). The registry keeps one `gitlab` row.

### Time and memory

| | measured (Orbital staging worker, m6a.2xlarge) |
|---|---|
| first boot to sign-in 200 | see the PR's validation numbers |
| GitLab steady | ~2.3 GiB |
| Runner | ~30 MiB idle (+ one pod per running job) |

The image is 3.8 GB: cache it in the scenario image to take the pull off the start time.

### Seeding a project

```bash
gitlabSeedRepo my-service ./apps/my-service
```

Creates `root/my-service` (private) if it is missing, then pushes the directory's files to `main` as
one commit on top of what is there. Re-running with unchanged files pushes nothing. Git reaches
GitLab through the cluster ingress on this host (`GITLAB_GIT_URL` overrides the base URL).

## Browser IDE (code-server)

code-server runs **in the learner container**, as the learner, where the browser terminal lands:
same files, same `kubectl` context, same tools. Its terminal is **zsh**, so it prints the same
greeting as the browser terminal.

```bash
startCodeServer            # opens $REPO_PATH on port 8443
startCodeServer ./src      # another folder
stopCodeServer
```

* **Install**: `installCodeServer` downloads the release **tarball** (`CODE_SERVER_VERSION`, pinned
  `4.141.0`) and verifies it against the **reviewed sha256** in `variables.sh`; any other version
  needs `CODE_SERVER_SHA256` or is refused. (The upstream `install.sh` reads `$ARCH`, which Orbital
  sets to `x86_64`, and fails.) `startCodeServer` installs it when missing. ~700 MB on disk.
* **Settings** (written once, a learner's edits are kept): no workspace-trust prompt, no Chat/agent
  panel, terminal profile zsh, no telemetry or update checks.
* **Abandoned tabs**: `--reconnection-grace-time` (`CODE_SERVER_RECONNECT_GRACE`, 600 s) ends what a
  closed browser tab keeps alive; without it each abandoned window kept its extension host and
  terminals (~350 MiB each).

### Authentication

| | `PASSWORD` set | `PASSWORD` unset |
|---|---|---|
| Orbital | `--auth password`, bound `0.0.0.0` | **refuses to start** |
| elsewhere | `--auth password`, bound `127.0.0.1` | `--auth none`, bound `127.0.0.1` |

`PASSWORD` (or `HASHED_PASSWORD`) is code-server's own variable; it is read from the environment,
never passed as an argument. Under Orbital a password is mandatory because a slot's ports on the
worker are reachable from the other learners' containers on the same host. Orbital starts
code-server with a password it generated, signs in server-side and gives the browser only the
session cookie. `CODE_SERVER_BIND` overrides the bind address.

### Registered as an app

Under Orbital, and only when it has a password, `startCodeServer` registers code-server as app
**`ide`** (namespace `ide`): a Service with no selector and an EndpointSlice pointing at the k3d
network's gateway — the learner container's network namespace — plus the usual `registerApp`
ingress. It therefore appears in the app registry Orbital reads, next to `gitlab` and the
training's apps. Outside Orbital it is not registered (`CODE_SERVER_REGISTER=1` forces it).

### Contract with Orbital

| | |
|---|---|
| start | as the learner, in `/workspaces/<repo>`: `PASSWORD=<generated>` in the environment, then `source .devcontainer/util/source_framework.sh; startCodeServer` |
| port | `CODE_SERVER_PORT`, default `8443` |
| health | `GET :8443/healthz` → 200 (`checkCodeServerRunning`) |
| stop | `stopCodeServer` |
| registry | app `ide`, namespace `ide`, service `ide:8443` |
| files | pid `~/.cache/code-server/pid`, log `~/.cache/code-server/code-server.log` |
