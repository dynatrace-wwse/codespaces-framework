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

## Not done yet

- No Orbital services installed. AWS CLI v2 is installed; nothing else.
- No `ORBITAL_ENV=staging` env file, no staging Redis, no nginx, no certificate.
- DNS for `staging.autonomous-enablements.whydevslovedynatrace.com` does not
  exist — point it at `35.176.95.18` in GCP Cloud DNS, then issue the cert with
  the existing `/etc/letsencrypt/hooks/gcp-dns-auth.sh` DNS-01 hooks.
- `environment.py`'s staging entry still has `template_instance_id = ""` — set
  it once a staging WORKER exists, so staging workers never inherit production
  worker-1's networking.
