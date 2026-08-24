# Tenant readiness checker

Public page an SE runs **before** registering a tenant, and the first thing to
reach for when a tenant misbehaves afterwards:

**https://autonomous-enablements-check.whydevslovedynatrace.com**

A thin Flask wrapper (`app.py`) around `check-tenant-setup.sh`, which is itself a
thin client of Orbital's `POST /api/deploy/preflight`.

**The checks do not live here.** They live in
`ops-server/dashboard/tenant_preflight.py`, and Register Tenant runs the same
function — so **adding or changing a check means editing that module, not this
script.** Rebuild the image only when the *rendering* changes.

That indirection is the whole point. This page used to re-implement the gate in
267 lines of bash. The two drifted (different document probe, different gen3 probe
host, skip-vs-fail, a different settings schema, no install probe at all), and on
2026-08-24 an SE saw this page go all-green and Register Tenant answer HTTP 412 in
the same minute. `dashboard/test_preflight_parity.py` now fails if the script
starts probing tenants directly again, or if either side grows its own scope list.

## Layout, and why

| Path | What |
|---|---|
| `check-tenant-setup.sh` | POSTs to Orbital and renders the report. **Canonical copy** — the checks themselves are in `dashboard/tenant_preflight.py` |
| `../check-tenant-setup.sh` | symlink to it, for running from the CLI |
| `app.py` | form + runner + one JSON audit line per run (never the secret) |
| `Dockerfile` / `k8s.yaml` | image + deployment |

The script must live *here*, not one level up: `docker build` follows a symlink
pointing out of the build context and copies a dangling link. Both copies used to
be real files and drifted — a fix landing in one and missing the other is exactly
how the page ended up unable to check the thing it was being recommended for.

## What it checks

1. **Scope catalog** — one scope-less `client_credentials` grant returns the client's
   ENTIRE granted catalog, so the missing scopes are a set difference rather than 15
   round-trips. A 400 on that bare grant means the id/secret is wrong, which is the only
   way to tell that apart from a missing scope (SSO returns a byte-identical 400 for both).
2. **Capabilities** — mints a real learner token, a real ActiveGate token, writes a
   real settings object, stores a real document, reads the app registry, and mints the
   account-scoped install bearer. A granted scope is not proof.
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
3 replicas. Current image: **1.6** (2026-08-24).

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


## Dependency: Orbital must be reachable from this cluster

The page now needs egress to
`https://autonomous-enablements.whydevslovedynatrace.com` (override with
`ORBITAL_URL`). Two things depend on it:

* `POST /api/deploy/preflight` — the checks.
* `GET /api/deploy/preflight-scopes` — the required-scope panel.

Both fail **loudly and honestly**. An unreachable Orbital renders *"NOT VERIFIED —
the check did not run"*, never a verdict, and the scope panel says it could not be
loaded rather than showing a baked-in copy. A stale list that looks authoritative is
the failure this design removes.

⚠️ Those two paths must stay in the anonymous-allowed nginx alternation on Orbital
(`nginx/ops-server.conf`, `location ~ ^/api/deploy/(token|…|preflight|preflight-scopes)$`).
The generic `^/api/deploy/` block requires an oauth2-proxy session; this page calls
server-to-server and has none, so a route that slips into the catch-all 401s in
production while passing every unit test. `test_preflight_parity.py` pins it.
