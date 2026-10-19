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
| `uninstallGitlab` | Removes the Runner, the ingress + registry row, and the namespace (data included); rc 1 if the namespace delete fails |
| `installCodeServer [version]` | Installs code-server from its release tarball, checksum-verified |
| `startCodeServer [dir]` | Starts code-server on `CODE_SERVER_PORT` (8443), opening `dir` (default: the repo) |
| `stopCodeServer` | Ends code-server and every extension host and terminal it started, and removes app `ide` from the registry |
| `registerCodeServer` | Registers code-server as app `ide` (run by `startCodeServer` under Orbital) |
| `checkCodeServerRunning` | `/healthz` answers 200 **and** the listener belongs to the code-server `startCodeServer` started |
| `uninstallCodeServer` | Stops it, removes the registration and the install (settings stay) |

## GitLab

### What is deployed

* **GitLab Omnibus CE** (`GITLAB_VERSION`, default `18.4.0-ce.0`) as **one Deployment** with one
  PVC (`gitlab-data`: config, data and logs) and one Service (`gitlab:80`). Omnibus, not the
  cloud-native Helm chart: measured on the same host it is ~1.5 GiB lighter (2.3 vs 3.85 GiB) and
  starts as fast.
* GitLab's own *memory-constrained* settings: Puma in single mode (`worker_processes = 0`), Sidekiq
  concurrency 10, and no Prometheus, container registry, KAS, Pages or Mattermost. No Git over
  SSH: Git uses HTTP(S) only. What is encrypted depends on the hop — the **learner's** clone and
  push through Orbital is HTTPS (TLS ends at Orbital); inside the cluster it is plain HTTP:
  `gitlabSeedRepo` pushes to the ingress on this host (`http://gitlab.<ip>.sslip.io`, pinned to
  `127.0.0.1`), and the Runner and its jobs clone from the Service `http://gitlab.gitlab.svc`.
* **GitLab Runner** from its own chart (`GITLAB_RUNNER_CHART_VERSION`, default `0.81.0` = runner
  18.4), **Kubernetes executor**, jobs run as pods in namespace `gitlab` (default image
  `alpine:3.20`). It is registered automatically as an instance runner that runs untagged jobs.
  A training whose pipeline deploys into other namespaces grants that RBAC itself.
* **CI job pods are fenced** (NetworkPolicy `gitlab/ci-job-egress`, enforced by k3s' embedded
  network-policy controller). A pipeline runs arbitrary code, so its pods (label
  `enablement-framework/ci-job=true`, set in the runner config — a job cannot change it) may reach
  any pod in the cluster, the Kubernetes API and the internet, but **no** private, link-local or
  loopback address outside the cluster (`10/8`, `172.16/12`, `192.168/16`, `100.64/10`,
  `169.254/16`, `127/8`). That keeps them off the learner container's own ports — code-server
  listens there — and off the cloud metadata endpoint. A training whose jobs must reach such an
  address adds its own policy.
* k3s enforces the policy about **1–2 s after** a pod starts — measured, long enough for a job's
  first script line or a `services:` entrypoint. So every job pod gets an **init container**
  (`wait-for-egress-fence`, runner `pod_spec` patch) that runs before all of them and waits until
  the node's kubelet port (inside the fenced range) stops answering — typically 2 s. A cluster
  that never enforces NetworkPolicy fails the job after 60 s rather than run it unfenced;
  `GITLAB_CI_FENCE=0` removes the gate.

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
  the token on **stdin** (`curl -H @-`), bounded by `GITLAB_API_TIMEOUT` (30 s). Minting a new
  token revokes the earlier framework tokens (`enablement-framework-*`), so one is live.
* **Runner token**: created through the API into secret `gitlab/gitlab-runner-secret`; a re-run
  verifies it with the value on stdin (`curl --data-urlencode token@-`), so it is in no process'
  arguments, outside or inside the pod.
* `gitlabSeedRepo` gives git the token through `GIT_ASKPASS` — never in a URL or on a command line,
  so it is not in `ps`, in `.git/config` or in shell history.

### Idempotent

Re-running `deployGitlab` keeps the password, the data, a token that still authenticates and a
runner GitLab still knows. It replaces the API token only when GitLab **rejects** it (HTTP 401); a
GitLab that does not answer (timeout, 5xx) fails the run instead of replacing a token that may be
fine. A runner token that no longer verifies fails the run too, with the command to clear it —
it never registers a second runner. GitLab's pod rolls only when its
configuration changed (for example a new external URL). The registry keeps one `gitlab` row.

### Time and memory

Measured in an Orbital-shaped sandbox on a staging worker (m6a.2xlarge, 2026-10-17):

| | |
|---|---|
| `deployGitlab`, cold host (3.8 GB image pull included) | 361 s: ready 226 s, API token 43 s, Runner 90 s |
| `deployGitlab` re-run with a config change (pod rolls) | 126 s |
| GitLab pod, steady | 2.4 GiB (`kubectl top`) |
| Runner, idle | 20 MiB (+ one pod per running job) |
| whole environment (K3d + ingress + ToDo + GitLab + Runner) | 4.1 GiB working set (1.1 GiB before GitLab) |
| 50 MB `git push` | 3–6 s; a pipeline job: ~14 s, queued 1–2 s |

The image is 3.8 GB: cache it in the scenario image to take the pull off the start time.

### Seeding a project

```bash
gitlabSeedRepo my-service ./apps/my-service
```

Creates `root/my-service` (private) if it is missing, then pushes the directory's files to `main` as
one commit on top of what is there. Re-running with unchanged files pushes nothing. Git reaches
GitLab through the cluster ingress on this host (`GITLAB_GIT_URL` overrides the base URL; git's
`http.curloptResolve` pins the ingress host to `127.0.0.1`, so no public DNS is involved). Clone and
push are bounded by `GITLAB_GIT_TIMEOUT` (600 s).

What is pushed: when the directory is a git work tree, the files git **tracks** (nothing
gitignored, no untracked scratch); otherwise every file. Never `.git`, and never a `.env*` file
— tracked or not: a training's `.devcontainer/.env` holds tenant credentials.

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
* **Memory**: 166 MiB idle; ~600 MiB with one window and a terminal open. Start to `/healthz`
  ~1.3 s.
* **Abandoned tabs**: `--reconnection-grace-time` (`CODE_SERVER_RECONNECT_GRACE`, 600 s) ends what a
  closed browser tab keeps alive; without it each abandoned window kept its extension host and
  terminals (~350 MiB each).

### Authentication

| | `PASSWORD` set | `PASSWORD` unset |
|---|---|---|
| Orbital | `--auth password`, bound `0.0.0.0` | **refuses to start** |
| elsewhere | `--auth password`, bound `127.0.0.1` | `--auth none`, bound `127.0.0.1`; a non-loopback `CODE_SERVER_BIND` **refuses to start** |

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
`stopCodeServer` removes the `ide` row again (the Service stays; the next start re-registers).

The gateway is not a pod, so no NetworkPolicy *on the IDE* can restrict who connects; the fence is
on the side that runs untrusted code — the CI job pods (see GitLab above) — and code-server's
password guards every other path.

### Contract with Orbital

| | |
|---|---|
| start | as the learner, in `/workspaces/<repo>`: `PASSWORD=<generated>` in the environment, then `source .devcontainer/util/source_framework.sh; startCodeServer` |
| port | `CODE_SERVER_PORT`, default `8443` |
| health | `GET :8443/healthz` → 200 (`checkCodeServerRunning` also checks the listener is ours) |
| stop | `stopCodeServer` (also deregisters `ide`) |
| registry | app `ide`, namespace `ide`, service `ide:8443` |
| files | pid `~/.cache/code-server/pid`, log `~/.cache/code-server/code-server.log` |
