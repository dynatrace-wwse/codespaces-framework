# Staging host

Provisioned 2026-08-26. Phase 3 of the environment-separation epic.

## What exists

| | |
|---|---|
| Instance | `i-0eb325415862bbcd2` — `autonomous-enablements-staging` |
| Type | `c7g.4xlarge` (16 vCPU / 30 GB / aarch64), mirroring the prod master |
| AMI | `ami-0c1de60a540962231`, Ubuntu 24.04.4 LTS |
| AZ / subnet | `eu-west-2c` / `subnet-42c9082b` (same VPC as prod) |
| Private IP | `172.31.38.222` |
| Elastic IP | `35.176.95.18` (`eipalloc-0dd937f13b180deb3`) |
| Root volume | 300 GB gp3, 3000 IOPS / 125 MB/s |
| Instance profile | `OrbitalFleetAutoscalerStaging` |
| Security group | `sg-093104049c36a0d42` (`orbital-staging`) |
| SSH | `ssh autonomous-enablements-staging` from the prod master |

## Protections

- **`disableApiTermination = True`.** Verified by actually attempting a
  terminate: `OperationNotPermitted ... may not be terminated`. To decommission
  the box this must be turned off deliberately first — which is the point.
- **`instanceInitiatedShutdownBehavior = stop`.** A `shutdown -h` inside the
  guest stops the instance rather than destroying it.
- **An Elastic IP, not an ephemeral public IP.** The staging DNS record must
  keep resolving across a stop/start; an ephemeral address is released on stop.

## Security group

Deliberately NOT the prod `launch-wizard-5` (`sg-022d32bd9ff4e3c97`), which is
shared by the master, both workers and an unrelated `agentic-development` box.
Sharing it would recreate exactly the coupling this epic removes, and would
mean a Phase 7 SSH change could not be rolled out to one environment at a time.

| Port | Source | Why |
|---|---|---|
| 443, 80 | `0.0.0.0/0` | Learners are worldwide and NOT on the Dynatrace VPN, and the app's AppEngine functions call in from Dynatrace SaaS egress. ACME http-01 needs 80. |
| 22 | `172.31.0.0/16` | **VPC only.** Reachable by hopping through the prod master. Phase 7 adds the Dynatrace VPN prefix list for direct access. |
| 6379, 8080, 8443, 32000-32099 | `172.31.0.0/16` | Staging Redis, FastAPI, webhook, nginx-fronted app tabs. |

Prod's SSH is still `0.0.0.0/0`; staging's is not. That is intentional — a new
box should not be born with the posture we are in the middle of removing.

## Isolation, verified end to end

Run from the staging box itself, against real production instances:

```
identity : arn:aws:sts::112258687663:assumed-role/OrbitalFleetAutoscalerStaging/i-0eb325415862bbcd2

SEE prod worker            -> env tag = prod        (describes are account-wide by design)
STOP prod worker           -> UnauthorizedOperation
TERMINATE prod worker      -> UnauthorizedOperation
STOP prod MASTER           -> UnauthorizedOperation
TERMINATE prod master      -> UnauthorizedOperation
```

Production after the test: all four instances in their expected states, API 200.

Seeing production is expected and unavoidable — `ec2:DescribeInstances` takes
no resource ARN and cannot be tag-conditioned in IAM. That is precisely why
`shared/environment.py` filters client-side; see `docs/iam/environment-isolation.md`.

## Installed (2026-08-26)

Built with `ops-server/setup.sh`, run as
`sudo ORBITAL_ENV=staging FRAMEWORK_BRANCH=main bash setup.sh`.

| Component | State |
|---|---|
| Redis | own instance, own 256-bit password, `bind 127.0.0.1 172.31.38.222`, **AOF on from birth** |
| nginx | active on 80/443, `server_name staging.…`, **no `http2`** |
| ops-dashboard | active, connected to staging Redis, control loop **DRY RUN** |
| ops-webhook | active |
| oauth2-proxy | **inactive — blocked**, see below |
| Stack | docker, k3d, kubectl, helm, node, gh, dtctl, python venv |

### `/home/ops/.env` — 26 keys, `0600 ops:ops`

Production credentials are **deliberately absent**, and this is verified rather
than assumed: no `COE_*`, no `SRO_*`, no `DT_PLATFORM_TOKEN`, no
`REMOTE_GRAIL_COE_TOKEN_ENC`, no `CODESPACES_TRACKER_TOKEN`. A staging mistake
cannot write to a production tenant or to production telemetry.

Inherited from production: the **sprint** tenant credentials (sprint is the
staging tenant, decision D2) and a GitHub token for repository operations.
Generated fresh on the box: `ORBITAL_TOKEN`, `WEBHOOK_SECRET`,
`OAUTH2_COOKIE_SECRET`, and the Redis password.

`CONTROL_LOOP_APPLY=0`. The control loop launches and terminates EC2; staging
must be observed making correct decisions before it is allowed to act on them.
The startup log confirms it: *"Control loop is in DRY RUN — it will log what it
would do and launch NOTHING."*

### Isolation, verified from the running stack

`GET /api/fleet` on staging returns **exactly one instance — itself**, while
production's returns its four. Staging's Redis password returns `WRONGPASS`
against production's Redis. The two control planes share an AWS account and see
each other's instances through `DescribeInstances`, and neither can act on nor
read the other's state.

## Two things setup.sh got wrong, now fixed

Running the installer surfaced defects that would have made staging **worse**
than production, all corrected in the same change:

- It cloned `--branch rfe/ops-server`, a branch that no longer exists.
- It set Redis `bind 0.0.0.0`, exposing the control plane's entire state on the
  public interface with a shared secret as the only protection. Now binds
  loopback plus the host's private address, as production does.
- The generated password was 128-bit and the file inherited the caller's umask,
  landing **world-readable** — the same `0644` `.env.generated` found on
  production earlier in this epic. Now 256-bit, and the file is created `0600`
  and owned by `ops` *before* anything is written to it.
- It left `appendonly no`; staging now has AOF from birth.

## nginx: a gotcha specific to a longer hostname

`staging.autonomous-enablements.…` is eight characters longer than production's
name and overflows nginx's default `server_names_hash_bucket_size 64`, so nginx
refuses to start with `could not build server_names_hash`. Raised to 128.
Production never hit this because its name is shorter — any environment with a
longer prefix will. `/var/cache/nginx/content-packs` must also be created by
hand; the `proxy_cache_path` directive does not create it.

## Not done yet

- **oauth2-proxy is blocked on a new GitHub OAuth app.** It needs
  `OAUTH2_CLIENT_ID` and `OAUTH2_CLIENT_SECRET` for an app whose callback is the
  staging host; `OAUTH2_COOKIE_SECRET` and `OAUTH2_GITHUB_ORG` are already set.
  Until it runs, requests through nginx to gated paths return **500**, not 401:
  the `auth_request` subrequest gets connection-refused (502), and a 502 is not
  caught by the config's `error_page 401` fallback. The app itself is healthy —
  `http://127.0.0.1:8080` answers 200 — so this is purely the SSO front door.
  After adding the two values, re-run `setup.sh` to render the config.
- **TLS is a self-signed placeholder** at `/etc/ssl/orbital-staging/`, valid 90
  days, so nginx could start before DNS exists. Replace with the real
  certificate once the A record is live.
- DNS for `staging.autonomous-enablements.whydevslovedynatrace.com` does not
  exist — point it at `35.176.95.18` in GCP Cloud DNS, then issue the cert with
  the existing `/etc/letsencrypt/hooks/gcp-dns-auth.sh` DNS-01 hooks and repoint
  `ssl_certificate` / `ssl_certificate_key` at `/etc/letsencrypt/live/<host>/`.
- `environment.py`'s staging entry still has `template_instance_id = ""` — set
  it once a staging WORKER exists, so staging workers never inherit production
  worker-1's networking.
