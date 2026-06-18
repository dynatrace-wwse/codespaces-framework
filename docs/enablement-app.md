
!!! example "The Enablement App — training delivery inside Dynatrace"
    The **Enablement App** (`my.dynatrace.enablements`) is the learner-facing surface of the
    framework: a Dynatrace App that lets a user browse the training catalog, launch a live
    environment, and work through the hands-on labs — **without ever leaving their Dynatrace
    tenant**. It is the product; the framework and [Orbital](ops-platform.md) are the engine.

---

## Where it fits

The framework defines *how one lab environment runs*. [Orbital](ops-platform.md) runs *all of them
at scale*. The **Enablement App** is the third side of the triangle — *how a learner actually starts
and consumes* a training, in-product.

```
   Learner (in a Dynatrace tenant)
        │  opens the "Enablements" Dynatrace App
        ▼
   ┌──────────────────────────────────────────────┐
   │  ENABLEMENT APP  (my.dynatrace.enablements)    │
   │   • catalog + training player (React)          │
   │   • api/orbital.function.ts  → Orbital REST    │
   │   • api/import-lab.function.ts → lab content    │
   │   • api/mintTrainingTokens   → scoped DT tokens │
   └──────────────────────────────────────────────┘
        │  POST /api/arena/provision (Bearer)
        ▼
   ┌──────────────────────────────────────────────┐
   │  ORBITAL  (ops-server)                         │
   │   RPUSH queue:test:amd64 → Worker Agent        │
   │   git clone <repo> → write .devcontainer/.env  │
   │   docker run inner devcontainer                │
   │   post-create.sh → post-start.sh               │
   └──────────────────────────────────────────────┘
        │  runs the SAME repo + .devcontainer
        ▼
   ┌──────────────────────────────────────────────┐
   │  CODESPACES FRAMEWORK                          │
   │   .devcontainer/ · functions.sh · variables.sh │
   │   sync/ keeps all 27 repos on one version      │
   └──────────────────────────────────────────────┘
```

The crucial point: **Orbital and a GitHub Codespace are not different runtimes.** Both run the exact
same training repo, the same `.devcontainer/devcontainer.json`, and the same `post-create.sh`. The
framework already knows which host it is on — `util/variables.sh` sets `INSTANTIATION_TYPE`
(`orbital`, `github-codespaces`, `remote-container`, …) and `detectRunEnvironment()` in
`util/functions.sh` branches app-URL construction accordingly (see
[Instantiation types](instantiation-types.md)). The Enablement App is simply a **new way to press
"start"** on top of that shared machinery.

---

## What the app does

| Capability | Backed by | Notes |
|---|---|---|
| **Catalog** of live trainings | `GET /api/arena/trainings` (Orbital) | Filtered per-tenant by the content-service profile |
| **Launch** a live environment | `POST /api/arena/provision` (Orbital) | Spins an Orbital Sysbox daemon job (k3d + DT Operator + demo apps) |
| **Lab content** (steps, questions) | `api/import-lab.function.ts` → GitHub → Document Service | Content only — never compute |
| **Per-training DT tokens** | `api/mintTrainingTokens.function.ts` | Scoped, session-TTL tokens on the learner's own tenant |
| **Interactive shell** | Orbital PTY bridge (WebSocket) | `${ORBITAL}/shell/<jobId>` |
| **Progress + telemetry** | Dynatrace state APIs + BizEvents | Cross-tenant training analytics |

The app talks to Orbital through a single backend proxy (`api/orbital.function.ts`) because browser
CSP blocks direct calls; that function adds the `orbital-config` Bearer token and brokers every
`/api/arena/*` action (provision, status, exec, shell-token, terminate, logs).

Full reverse-engineering of the data flow, plus the app's runtime/perf review, live in the app repo:

- `dynatrace-app-enablements/docs/CODESPACES_DIRECT_LAUNCH_ANALYSIS.md` — architecture + data flow + the Codespaces toggle.
- `dynatrace-app-enablements/docs/OPTIMIZATION_REVIEW.md` — app-internal rendering / N+1 / multi-tenant scale.
- `dynatrace-app-enablements/docs/multi-tenancy-tokens.md` — the minted-token model.

---

## Launching in GitHub Codespaces from the app (design)

A planned admin toggle lets an operator switch the app's launch backend from **Orbital-hosted
compute** to a **GitHub Codespace running in the learner's own GitHub account** — the user picks a
machine, GitHub spins it up and bills it, and **Orbital is repurposed as a relay** that forwards the
Codespace's terminal, logs, and app URL back into the app player. The player UI barely changes.

**This is feasible and is the correct GitHub model:** a **per-user OAuth token** (scope `codespace`)
creates a Codespace **owned by and billed to that user**, on a user-chosen machine. No shared/bot
token is involved.

```
 App: mintTrainingTokens + GitHub OAuth (codespace scope, per user)
   │
   ▼
 Orbital (RELAY, no compute):  holds the user's token
   • PUT /user/codespaces/secrets/DT_*        (per-tenant isolation)
   • POST /repos/{org}/{repo}/codespaces      (as the user → user-owned)
   • gh cs ssh -c {name} → PTY bridge         (in-app terminal; sshd installed on demand by functions.sh)
   • gh codespace logs                        (log relay; same SSH channel)
   • ports visibility 80:public               (app URL relay)
   • gh api -X DELETE user/codespaces/{name}  (terminate from the app)
   │
   ▼
 User-owned Codespace: framework devcontainer → k3d + DT Operator + apps
```

!!! info "Why a shared server token still can't create it"
    `POST /repos/{owner}/{repo}/codespaces` is **owned by + billed to the authenticating user** and
    has **no on-behalf-of**, so a bot PAT can't create a Codespace the learner can open — even for a
    **public** repo (auth is tied to the *creating identity*). That is exactly why the design uses a
    **per-user** OAuth token. The create call also takes **no env/secrets**, so DT creds are written
    as the **user's** Codespaces secrets before create — repo/org secrets would break per-tenant
    isolation (CoE-token leak), so **user**-scope is required.

The framework's `secrets: { DT_ENVIRONMENT, DT_OPERATOR_TOKEN, DT_INGEST_TOKEN }` contract is
unchanged; `post-create.sh` fails closed if they are absent, so Orbital sets them before create. Full
architecture, sequence diagrams, relay mechanics, and production-grade code:
`dynatrace-app-enablements/docs/CODESPACES_DIRECT_LAUNCH_ANALYSIS.md`.

---

## Key Links

| Resource | URL |
|---|---|
| **Enablement App repo** | `dynatrace-app-enablements/` |
| **Orbital — Ops Platform** | [ops-platform.md](ops-platform.md) |
| **Instantiation types** | [instantiation-types.md](instantiation-types.md) |
| **Lab Registry (The Hub)** | https://dynatrace-wwse.github.io/ |
| **COE Tenant** | https://geu80787.apps.dynatrace.com |

<div class="grid cards" markdown>
- [← Orbital — Ops Platform](ops-platform.md)
- [What's next? →](whats-next.md)
</div>
