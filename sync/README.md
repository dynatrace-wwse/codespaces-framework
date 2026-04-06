# Sync CLI

Framework version management and repo migration tool for the `dynatrace-wwse` organization.

All commands run from the `codespaces-framework` directory:

```bash
cd codespaces-framework
PYTHONPATH=. python3 -m sync.cli <command> [options]
```

## Workflow: Migrating repos to the versioned pull model

### 1. Validate current state

```bash
# Check repos.yaml schema, GitHub accessibility, and local clone state
PYTHONPATH=. python3 -m sync.cli validate

# Validate a specific repo
PYTHONPATH=. python3 -m sync.cli validate --repo enablement-dynatrace-log-ingest-101
```

Shows: devcontainer.json validation, Category A leftovers (framework files that should be removed), template freshness (source_framework.sh, Makefile).

### 2. Dry-run migration

```bash
# Preview what migrate would do across all sync-managed repos
PYTHONPATH=. python3 -m sync.cli migrate --dry-run

# Preview for a specific repo
PYTHONPATH=. python3 -m sync.cli migrate --repo enablement-codespaces-template --dry-run
```

### 3. Run migration

```bash
# Migrate all sync-managed repos in repos.yaml
PYTHONPATH=. python3 -m sync.cli migrate

# Migrate a specific repo
PYTHONPATH=. python3 -m sync.cli migrate --repo enablement-codespaces-template
```

What migrate does per repo:
- Removes **Category A files** (framework-owned): `functions.sh`, `variables.sh`, `greeting.sh`, `test_functions.sh`, `makefile.sh`, `helper.sh`, `Dockerfile`, `entrypoint.sh`, `kind-cluster.yml`, `apps/`, `p10k/`, `yaml/`
- Installs/updates **thin Makefile** (delegates to cached `makefile.sh`)
- Installs/updates **source_framework.sh** (versioned pull + two-tier cache)
- Migrates **mkdocs.yaml** to `INHERIT: mkdocs-base.yaml` pattern
- Extracts **RUM snippet** from `main.html` into `mkdocs.yaml`
- Updates **deploy-ghpages.yaml** to fetch `mkdocs-base.yaml` at framework version
- Relocates **.env** from `runlocal/.env` to `.devcontainer/.env`
- Updates **.vscode/mcp.json**, **.gitignore**, **devcontainer.json** comment, **CI workflows**, and **docs** to the new `.env` path

### 4. Review changes

```bash
# Check diffs per repo
for repo in enablement-dynatrace-log-ingest-101 remote-environment enablement-codespaces-template; do
  echo "── $repo ──"
  git -C ../$repo diff --stat
  echo
done
```

### 5. Revert if needed

```bash
# Revert all repos to last committed state
PYTHONPATH=. python3 -m sync.cli revert

# Revert a specific repo
PYTHONPATH=. python3 -m sync.cli revert --repo enablement-codespaces-template
```

### 6. Test locally

From a migrated repo:

```bash
cd ../enablement-codespaces-template/.devcontainer

# Create .env with your secrets (or empty file)
touch .env

# Start container (bootstraps cache + runs docker)
make start

# Full clean restart (kill containers, clear cache, re-bootstrap)
make clean-start
```

### 7. Push updates across repos

After tagging a new framework version:

```bash
# Preview which repos need updating
PYTHONPATH=. python3 -m sync.cli push-update --framework-version 1.2.5 --dry-run

# Create PRs (clones if needed, pulls main, branches, commits, pushes, creates PR)
PYTHONPATH=. python3 -m sync.cli push-update --framework-version 1.2.5
```

---

## All commands

| Command | Description |
|---------|-------------|
| `migrate` | Migrate repos to versioned pull model. Iterates `repos.yaml`, removes framework files, installs templates. |
| `validate` | Validate `repos.yaml` schema, GitHub accessibility, and local clone state (devcontainer.json, templates, migration status). |
| `push-update` | Bump `FRAMEWORK_VERSION` across repos. Local-first: clone, pull main, branch, update templates, push, create PR. |
| `revert` | Revert uncommitted changes in repos (`git checkout -- . && git clean -fd`). |
| `status` | Show version drift across repos (which repos are behind the latest framework version). |
| `diff` | Preview what `push-update` would change. |
| `list` | List registered repos from `repos.yaml`. Supports `--sync-managed`, `--ci-enabled`, `--json`. |
| `tag` | Create combined version tags (`vX.Y.Z_A.B.C`) after sync PRs merge. |
| `bump-repo-version` | Increment a repo's version component (patch/minor/major). |
| `migrate-mkdocs` | Standalone mkdocs.yaml migration to INHERIT pattern (also runs as part of `migrate`). |
| `generate-registry` | Generate HTML registry page from `repos.yaml`. |

## Key files

| File | Purpose |
|------|---------|
| `sync/cli.py` | CLI entry point and argument parsing |
| `sync/core/repos.py` | `repos.yaml` parsing, validation, `RepoEntry` dataclass |
| `sync/core/version.py` | Version parsing, bumping, `FRAMEWORK_VERSION` extraction |
| `sync/core/github_api.py` | GitHub API wrapper via `gh` CLI |
| `sync/core/local_git.py` | Local git operations (clone, pull, branch, commit, push) |
| `sync/commands/migrate.py` | Migration logic, Category A/B file lists, templates (source_framework.sh, Makefile) |
| `sync/commands/push_update.py` | Local-first push-update workflow |
| `sync/commands/validate.py` | Schema + GitHub + local clone validation |
| `sync/commands/revert.py` | Revert uncommitted changes |
| `repos.yaml` | Registry of all repos with metadata (status, maintainer, image_tier, tags) |

## Image tiers

Defined per repo in `repos.yaml` via `image_tier` (default: `k8s`):

| Tier | Description |
|------|-------------|
| `minimal` | Core framework only |
| `k8s` | Core + Kind cluster, entrypoint, Dynakube yaml templates |
| `ai` | Same as k8s (extensible for future AI-specific files) |

## What stays in each repo after migration

```
.devcontainer/
  devcontainer.json      # Container config (image, runArgs, secrets)
  .env                   # Secrets for local runs and MCP (gitignored)
  post-create.sh         # Repo-specific setup (custom per repo)
  post-start.sh          # Repo-specific post-start (custom per repo)
  Makefile               # Thin wrapper — delegates to cached makefile.sh
  .cache/                # Framework cache (gitignored, auto-populated)
  util/
    source_framework.sh  # Versioned pull mechanism (FRAMEWORK_VERSION pin)
    my_functions.sh      # Repo-specific custom functions
  test/
    integration.sh       # Repo-specific integration tests
  manifests/             # Repo-specific k8s manifests (if any)
  dynatrace/             # Repo-specific Dynatrace config (if any)
```

Everything else comes from the framework cache at the pinned `FRAMEWORK_VERSION`.
