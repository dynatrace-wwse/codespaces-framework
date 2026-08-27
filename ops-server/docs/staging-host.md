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

---

## DNS and TLS (done 2026-08-27)

### The name already resolved -- to production

Before any record was added, `staging.autonomous-enablements...` **already
answered `18.134.158.252`**, because the zone carries a wildcard
`*.autonomous-enablements` A record pointing at the production box. Anything
aimed at the staging hostname -- an OAuth callback, a health check, a test
harness -- would have hit production instead, with no error to notice. Check
for a shadowing wildcard before assuming an unresolved name is unreachable.

The wildcard is load-bearing (it serves the per-slot app tabs via
`subdomain_url`) and was left alone.

### Delegated child zone -- why not just an A record in the parent

`whydevslovedynatrace.com` holds **production's** A record. A certbot
credential with write access to that zone could repoint production DNS, so a
per-zone grant on the parent is not isolation -- it is a staging box holding a
key to production's name.

Staging therefore gets its own delegated zone:

| Zone | Holds |
|---|---|
| `whydevslovedynatrace-com` (parent) | prod A, wildcard A, **NS delegation** for the staging name |
| `staging-autonomous-enablements` (child) | staging A + its `_acme-challenge` TXT |

Cloud DNS refuses `NS` and `A` at the same name, so the parent's A record must
be **deleted before** the NS delegation is added. Create the A record in the
child first and the name never stops resolving.

### Credential -- two grants, deliberately split

`orbital-staging-certbot@sales-engineering-emea.iam.gserviceaccount.com`

| Scope | Role | Why |
|---|---|---|
| child zone only | `roles/dns.admin` | write the ACME TXT |
| project | custom `orbitalStagingCertbotZoneLookup` (`dns.managedZones.list`) | `certbot-dns-google` resolves a zone id by listing zones; without it every issuance 403s. Lists zone **names** only -- no record read, no write. |

`roles/dns.reader` at project level would also have fixed the 403 and was not
used: it would let staging read every record in every zone for no benefit.

**Verified in both directions, not assumed** -- as the service account:
write to the child zone succeeded; write to the parent zone returned
`HTTPError 403: Forbidden`.

### Issuance

`certbot-dns-google` (`python3-certbot-dns-google`), not the manual hooks
production uses. Production's hooks exist to accumulate two challenge values
under one `_acme-challenge` name (apex + wildcard share it) -- staging has a
single name and no such problem, and the plugin reads the service-account JSON
directly, so staging needs no `gcloud` install.

```bash
certbot certonly --dns-google   --dns-google-credentials /etc/letsencrypt/gcp-staging-certbot.json   --dns-google-propagation-seconds 60 --key-type ecdsa   -d staging.autonomous-enablements.whydevslovedynatrace.com
```

Key at `/etc/letsencrypt/gcp-staging-certbot.json`, `600 root:root`. The copy
used to transfer it was shredded on both ends.

### Renewal -- the part that actually matters

This domain lost ~90 days to a renewal that failed **silently**
(`authenticator = manual` with no auth hook). Two things close that here:

1. `certbot renew --dry-run` was run and reported
   *"Congratulations, all simulated renewals succeeded"*. `certbot.timer` is
   `enabled`.
2. **Deploy hook** `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh` --
   renewal writes new files but nginx serves the in-memory certificate until
   reloaded. Without the hook the renewal succeeds and the box still serves the
   expiring cert.

`orbital-inventory.sh`'s *"all certs valid for >30 days"* invariant currently
covers production hosts only. **Staging is not yet in the drift check.**

### App-tab wildcard (done 2026-08-27)

Staging inherited nginx server blocks whose `server_name` regex matched
**production's** wildcard, so they were dead here: those names resolve to prod
and never arrive. Note the earlier domain substitution *did* fix the CSP
`frame-ancestors` line, because that is a plain string -- only the two regexes,
with their escaped dots, were missed. Renaming a domain in this file is not one
find-and-replace.

| Piece | State |
|---|---|
| `*.staging.autonomous-enablements...` A record | child zone -> 35.176.95.18 |
| certificate | expanded to cover apex **and** wildcard |
| `server_name` regexes (~666, ~704) | re-anchored onto `.staging.` |

The certificate expansion is the case the earlier outage was about: apex and
wildcard both validate under the **same** `_acme-challenge.staging...` name, so
two TXT values must be live at once. `certbot-dns-google` handles that
correctly -- precisely why the plugin is used here rather than hand-rolling the
accumulation logic production's manual hooks need.

Verified: `slot42.staging...` reaches the staging box with a chain that
validates without `-k`, and answers `400 {"detail":"missing or invalid
X-App-Subdomain header"}` -- byte-identical to production's answer for a
nonexistent slot, which is what shows the block is wired rather than merely
present.

The deploy hook now captures `nginx -t` and re-emits it only on failure;
otherwise nginx's success banner goes to stderr and certbot labels every
healthy renewal *"deploy-hook ran with error output"*.

### Drift detection (done 2026-08-27)

Staging has its own baseline at `/var/lib/orbital-inventory/baseline.md` and
its own copy of the script -- not a section inside production's, because the
inventory reads the host it runs on.

`orbital-inventory.sh` had to be fixed first: its worker list defaulted to
**production's** two workers, so running it on staging would have ssh'd into
production and reported prod's systemd state, OneAgent mode and git refs as
staging's. The default now derives from `ORBITAL_ENV`.

Drift detection here is mutation-tested, not assumed: flipping a service state
in the baseline is reported as a diff, and the restored baseline is clean.

**`snapshot <48h old` reads NO on staging and that is correct** -- there are no
staging backups and nothing yet worth backing up. The check diffs against the
baseline, so a stable NO is not drift; it alarms only if it changes.

### Disarmed: `ops-nightly.timer`

Found `enabled` but `inactive`, with `OnCalendar=*-*-* 02:00:00`,
`Persistent=true`, **no stamp file**, and a live GitHub token in
`/home/ops/.env`. That combination means the next reboot of this box would
activate the timer and fire it *immediately*, running
`nightly.scheduler --include-framework` against the real repositories --
concurrently with production's own 02:00 nightly, which is `active`.

Disabled (`systemctl disable --now`). D9 does move nightly here, but as a
coordinated change: disable on production, provision a dedicated `pool=test`
worker, then enable. Inheriting it by accident on a reboot is not that.

Other inherited timers audited at the same time: `ops-sync-daemon`,
`ops-gen2scan`, `orbital-backup` and `orbital-restore-drill` are
disabled/inactive; `ops-docker-cleanup` and `certbot` are enabled and active,
which is correct.
