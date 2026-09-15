# Audit

Fleet-wide audit of the enablement repositories: what each one contains, which
documentation pages carry Gen2 (classic UI) references, and how each repo is
configured. It produces the audit table Orbital serves at `GET /audit`.

| Path | What it is | Maintained? |
|---|---|---|
| `fetch-data.sh` | Pulls per-repo configuration and metadata from GitHub into `data/`. Carries its own hardcoded repository list. | Yes — run by Orbital's `POST /api/audit/refresh`. |
| `fetch-docs.sh` | Pulls each repo's `docs/*.md` into `data/`. Reads `repos.yaml`. | Yes |
| `phase1-scan.py` | Classifies documentation pages by Gen2/Gen3 risk; writes `phase1-results.json`. | Yes |
| `generate-html.py` | Renders `master-table.html` from `data/`. | Yes — run by Orbital's `POST /api/audit/refresh`. |
| `phase1-results.json`, `master-table.html` | Generated output, committed. | Regenerated |
| `data/` | **Generated snapshot of 27 repositories, committed.** Last refreshed 2026-05-20 (`c218230`). | **No — see below** |

## About `data/`

`data/` is a committed snapshot, not authored documentation. It is also what Orbital
falls back to when a live fetch fails — `POST /api/audit/refresh` logs a warning and
regenerates the table from whatever is on disk — so it is not dead weight, and it
cannot simply be deleted or gitignored without changing that fallback first.

It is, however, stale and drifting from the fleet. `fetch-data.sh` carries a
hardcoded 27-repository list rather than reading `repos.yaml`, so a refresh does not
close the gap: the snapshot misses five repositories that are `status: active` today
— including `enablement-kubernetes-101` — and still carries one that `repos.yaml` no
longer lists at all.

It is also the largest single source of duplicated Markdown in this repository: 381
of the 426 tracked `.md` files live under `data/`. Twenty-one of them are a
four-month-old copy of this repository's own `docs/` pages — all 15 nav pages differ
from the live versions, as do three of the six snippets — so a full-text search of
this repository returns each page twice, once at a stale version.

## Who consumes `audit/data/` — evidence

Three files in the private Orbital repository read from this directory:

| File | Line | How it uses `data/` |
|---|---|---|
| `dashboard/app.py` | 2822 | `_AUDIT_DATA_DIR = FRAMEWORK_DIR / "audit" / "data"` — fixed path, no override |
| `dashboard/app.py` | 2833, 2869 | passes `AUDIT_DATA_DIR=str(_AUDIT_DATA_DIR)` when invoking `fetch-data.sh` and `generate-html.py` |
| `audit/generate-html.py` | 17 | `DATA_DIR = os.environ.get("AUDIT_DATA_DIR", os.path.join(SCRIPT_DIR, "data"))` — reads the env var Orbital always sets |
| `audit/fetch-data.sh` | 6 | `OUTPUT_DIR="${AUDIT_DATA_DIR:-$(dirname "$0")/data}"` — writes to the same path |

**`GET /audit`** (Orbital's public audit page) calls `_generate_audit_html()`, which
runs `generate-html.py` against whatever is in `data/` right now. There is no live
fetch in this path — it reads the committed snapshot. `POST /api/audit/refresh`
calls `_fetch_audit_data()` first, then `_generate_audit_html()`. If the fetch
fails (network, token, timeout), the committed data remains on disk and is used
without any error to the user.

Moving `audit/data/` breaks both `app.py:2822` (hardcoded) and
`generate-html.py`'s default. Any move requires a coordinated change in Orbital's
private repository.

**Drift as of 2026-09-15:**

| | Repos |
|---|---|
| In `data/` but NOT in `repos.yaml` | `demo-agentic-ai-with-nvidia` |
| In `repos.yaml` but NOT in `data/` | `enablement-app-training-template`, `enablement-bindplane-logs`, `enablement-dtwiz-101`, `enablement-dynatrace-ai-mcp`, `enablement-kubernetes-101` |

Root cause: `fetch-data.sh` hardcodes 27 repositories rather than reading
`repos.yaml`. `fetch-docs.sh` already reads `repos.yaml` — this inconsistency is
the fix needed.

**Secrets audit (2026-09-15):** no credentials, private IPs, or real tenant IDs
found in `data/`. The `sprint.dynatracelabs.com` and `live.dynatrace.com`
references are public lab placeholder values from the repos' own documentation.

## Recommendation

Three options, in preference order:

**1. Fix the list gap and refresh (immediate, low cost).** Change `fetch-data.sh`
to read its repository list from `repos.yaml`, the same way `fetch-docs.sh` already
does. Then run `POST /api/audit/refresh` on Orbital and commit the updated snapshot.
This closes the five-repo gap and removes the orphaned `demo-agentic-ai-with-nvidia`
entry. No Orbital code change required.

**2. Stop committing `audit/data/`, fix Orbital's fallback (medium cost).** Change
`app.py:_generate_audit_html` to return a "data not yet fetched — trigger
`POST /api/audit/refresh`" page when `data/` is empty, rather than generating a
stale HTML table. Add `audit/data/` to `.gitignore`. This removes 381 of the
repository's 428 tracked `.md` files and eliminates the stale-snapshot false-hit
problem permanently. Requires a coordinated change in Orbital (private repo) before
the gitignore lands here.

**3. Leave it as-is (no cost).** The `audit/README.md` (this file) now documents
what `data/` is and why it exists. A reader who reaches this README understands the
situation. The confusion cost is low; the correctness cost is the growing drift from
the fleet.

Do not gitignore `data/` before Option 2's Orbital change is deployed — `GET /audit`
would generate from an empty directory and the result is undefined behaviour in the
current code.
