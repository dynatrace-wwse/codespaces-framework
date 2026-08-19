# Tenant readiness checker

Public page an SE runs **before** registering a tenant, and the first thing to
reach for when a tenant misbehaves afterwards:

**https://autonomous-enablements-check.whydevslovedynatrace.com**

A thin Flask wrapper (`app.py`) around `check-tenant-setup.sh`. The page renders
whatever the script prints, so **adding a check means editing the script only** —
no Python change, no template change.

## Layout, and why

| Path | What |
|---|---|
| `check-tenant-setup.sh` | the checks. **Canonical copy** |
| `../check-tenant-setup.sh` | symlink to it, for running from the CLI |
| `app.py` | form + runner + one JSON audit line per run (never the secret) |
| `Dockerfile` / `k8s.yaml` | image + deployment |

The script must live *here*, not one level up: `docker build` follows a symlink
pointing out of the build context and copies a dangling link. Both copies used to
be real files and drifted — a fix landing in one and missing the other is exactly
how the page ended up unable to check the thing it was being recommended for.

## What it checks

1. **Scopes granted** — does SSO issue a bearer for each one.
2. **Capabilities** — mints a real learner token, a real ActiveGate token, writes a
   real settings object, stores a real document. A granted scope is not proof.
3. **Outbound connections** — the JS-runtime allowlist. *Absence of a list does not
   mean outbound is open* (measured on `uxn36332`, 2026-08-19: blocked with no
   settings object at any scope), so a missing object is reported as unproven, not
   as fine.
4. **Effective permissions** — `effective-permissions:resolve`. SSO stamps scope
   names without an entitlement check, so section 1 can pass while every call using
   the scope is refused.

⚠️ Section 4's bearer **must carry the permissions being asked about**. The API
answers for the *presented token*, not for what the client could obtain: ask with
an `app-engine:apps:run`-only bearer and every answer is `false`, including for
permissions the same client had just used successfully. That inverts the whole
section into a permanent false alarm.

## Deploy

```bash
cd ops-server/tools/tenant-check-page
gcloud config set project sales-engineering-emea
gcloud builds submit --tag eu.gcr.io/sales-engineering-emea/tenant-check:<next> .
kubectl -n tenant-check set image deploy/tenant-check \
  tenant-check=eu.gcr.io/sales-engineering-emea/tenant-check:<next>
kubectl -n tenant-check rollout status deploy/tenant-check
```

Cluster: GKE `hot-diagnostics-beta`, zone `europe-central2-a`, project
`sales-engineering-emea` — the same cluster and ingress as codespaces-tracker.
3 replicas. Current image: **1.5** (2026-08-19).

**Verify the rollout reached every replica**, because a partial rollout serves old
answers to some visitors:

```bash
for p in $(kubectl -n tenant-check get pods -l app=tenant-check -o name); do
  kubectl -n tenant-check exec ${p#pod/} -- \
    grep -c "Effective permissions" /srv/check-tenant-setup.sh
done
```

## Related tools in `..`

- `fix-outbound-allowlist.sh` — diagnose, and repair, a tenant's outbound
  allowlist. `--apply` widens an enforced list; `--create` writes one where none
  exists and requires that you have SEEN the app report a block.
- `apac-followup-status.sh` — reads Orbital's deploy audit and reports which of the
  APAC-bootcamp tenants have healed, so nobody has to be chased for a reply.
