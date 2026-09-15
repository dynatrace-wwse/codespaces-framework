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

Deciding what to do with it — refresh it, freeze it under a dated name, or stop
committing it and change Orbital's fallback — belongs with the audit's owner and is
left as a follow-up. See [../notes/README.md](../notes/README.md) for the briefs and
plans that produced this directory.
