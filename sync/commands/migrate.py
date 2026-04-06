"""sync migrate — Migrate a framework-based repo to the versioned pull model.

Operates locally on a cloned repo. Run from the repo root or pass --repo-path.
Handles repos already using the framework (have functions.sh, variables.sh, etc.).
"""

import shutil
import sys
from pathlib import Path

# Category A files — framework-owned, pulled from cache at runtime.
# These should be REMOVED from individual repos after migration.
CATEGORY_A_FILES = [
    ".devcontainer/util/functions.sh",
    ".devcontainer/util/variables.sh",
    ".devcontainer/util/greeting.sh",
    ".devcontainer/util/test_functions.sh",
    ".devcontainer/makefile.sh",
    ".devcontainer/runlocal/helper.sh",
]

CATEGORY_A_DIRS = [
    ".devcontainer/apps",
    ".devcontainer/p10k",
]

# Category B files — framework-owned, but stay in each repo as thin wrappers.
# These are REPLACED with framework templates during migration.
CATEGORY_B_FILES = [
    ".devcontainer/Makefile",
]

# ── Templates ──

SOURCE_FRAMEWORK_TEMPLATE = r'''#!/bin/bash
# Versioned framework pull mechanism
# sync push-update updates the FRAMEWORK_VERSION line only.
#
# Two modes:
#   DEV MODE:   Local functions.sh exists -> source directly (for codespaces-framework development)
#   CACHE MODE: No local files -> two-tier cache (host cache -> container cache -> git clone)
#
# Two-tier cache:
#   HOST_CACHE:      $REPO_PATH/.devcontainer/.cache/dt-framework/<version>/
#                    Lives inside the volume-mounted repo dir -> persists across container rebuilds
#   CONTAINER_CACHE: $HOME/.cache/dt-framework/<version>/
#                    Local to the container -> fast access, lost on container rebuild

# Framework version pin — sync push-update updates this line
FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-%s}"

REPO_PATH="$(pwd)"
RepositoryName="$(basename "$REPO_PATH")"

# -- DEV MODE: local files exist -> source directly, no cache --
if [ -f "${REPO_PATH}/.devcontainer/util/functions.sh" ]; then
  FRAMEWORK_CACHE=""
  FRAMEWORK_APPS_PATH="${REPO_PATH}/.devcontainer/apps"
  export FRAMEWORK_VERSION REPO_PATH RepositoryName FRAMEWORK_CACHE FRAMEWORK_APPS_PATH

  source "${REPO_PATH}/.devcontainer/util/functions.sh"
  return 0 2>/dev/null || exit 0
fi

# -- CACHE MODE: enablement repo (no local framework files) --
HOST_CACHE="${REPO_PATH}/.devcontainer/.cache/dt-framework/${FRAMEWORK_VERSION}"
CONTAINER_CACHE="${HOME}/.cache/dt-framework/${FRAMEWORK_VERSION}"
FRAMEWORK_CACHE="${CONTAINER_CACHE}"
FRAMEWORK_APPS_PATH="${FRAMEWORK_CACHE}/.devcontainer/apps"
export FRAMEWORK_VERSION REPO_PATH RepositoryName FRAMEWORK_CACHE FRAMEWORK_APPS_PATH

# Tier 1: Container cache exists -> use it directly
if [ -f "${CONTAINER_CACHE}/.complete" ]; then
  source "${FRAMEWORK_CACHE}/.devcontainer/util/functions.sh"
  return 0 2>/dev/null || exit 0
fi

# Tier 2: Host cache exists -> copy to container cache
if [ -f "${HOST_CACHE}/.complete" ]; then
  echo "Copying framework v${FRAMEWORK_VERSION} from host cache..."
  mkdir -p "${CONTAINER_CACHE}"
  cp -a "${HOST_CACHE}/." "${CONTAINER_CACHE}/"
  source "${FRAMEWORK_CACHE}/.devcontainer/util/functions.sh"
  return 0 2>/dev/null || exit 0
fi

# Tier 3: Neither cache exists -> git clone into host cache, then copy to container cache
echo "Pulling framework v${FRAMEWORK_VERSION}..."
if ! (
  mkdir -p "$(dirname "${HOST_CACHE}")" && \
  git clone --depth 1 --filter=blob:none --sparse \
    -b "${FRAMEWORK_VERSION}" \
    https://github.com/dynatrace-wwse/codespaces-framework.git \
    "${HOST_CACHE}" 2>/dev/null && \
  cd "${HOST_CACHE}" && \
  git sparse-checkout set \
    .devcontainer/util \
    .devcontainer/p10k \
    .devcontainer/test \
    .devcontainer/apps \
    .devcontainer/Makefile \
    .devcontainer/makefile.sh \
    .devcontainer/runlocal && \
  touch "${HOST_CACHE}/.complete"
); then
  echo "Failed to pull framework v${FRAMEWORK_VERSION} -- check network and retry"
  return 1 2>/dev/null || exit 1
fi

# Copy host cache to container cache
mkdir -p "${CONTAINER_CACHE}"
cp -a "${HOST_CACHE}/." "${CONTAINER_CACHE}/"

source "${FRAMEWORK_CACHE}/.devcontainer/util/functions.sh"
'''

THIN_MAKEFILE = r'''# Thin Makefile — bootstraps the framework cache and delegates to makefile.sh
# The actual build/run logic lives in the framework (pulled at FRAMEWORK_VERSION).
# For local Docker usage: cd .devcontainer && make start

FRAMEWORK_VERSION := $(shell grep -oP ':-\K[^}"]+' util/source_framework.sh | head -1)
CACHE := .cache/dt-framework/$(FRAMEWORK_VERSION)

# Bootstrap: populate host cache if missing
$(CACHE)/.complete:
	@echo "Bootstrapping framework v$(FRAMEWORK_VERSION)..."
	@mkdir -p $(dir $(CACHE))
	@git clone --depth 1 --filter=blob:none --sparse \
		-b $(FRAMEWORK_VERSION) \
		https://github.com/dynatrace-wwse/codespaces-framework.git \
		$(CACHE) 2>/dev/null
	@cd $(CACHE) && git sparse-checkout set \
		.devcontainer/util \
		.devcontainer/p10k \
		.devcontainer/test \
		.devcontainer/apps \
		.devcontainer/Makefile \
		.devcontainer/makefile.sh \
		.devcontainer/runlocal
	@touch $(CACHE)/.complete
	@echo "Framework v$(FRAMEWORK_VERSION) cached."

start: $(CACHE)/.complete
	@bash -c 'source $(CACHE)/.devcontainer/makefile.sh; start'

build: $(CACHE)/.complete
	@bash -c 'source $(CACHE)/.devcontainer/makefile.sh; build'

build-nocache: $(CACHE)/.complete
	@bash -c 'source $(CACHE)/.devcontainer/makefile.sh; buildNoCache'

buildx: $(CACHE)/.complete
	@bash -c 'source $(CACHE)/.devcontainer/makefile.sh; buildx'

integration: $(CACHE)/.complete
	@bash -c 'source $(CACHE)/.devcontainer/makefile.sh; integration'

clean-cache:
	@rm -rf .cache/dt-framework
	@echo "Framework cache cleared."
'''

OVERRIDES_MAIN_HTML = """\
{% extends "base.html" %}

{% block libs %}
  {{ super() }}
  {% if config.extra.rum_snippet %}
  <script type="text/javascript"
    src="{{ config.extra.rum_snippet }}"
    crossorigin="anonymous"></script>
  {% endif %}
{% endblock %}
"""

DEPLOY_GHPAGES_TEMPLATE = """\
name: deploy mkdocs to github pages
run-name: ${{ github.event.head_commit.message }}  - deploy github pages
permissions:
  contents: write
on:
  push:
    branches:
      - main
      - docs/*
jobs:
  deploy-github-pages:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
    - name: Checkout repository code
      uses: actions/checkout@v4
    - uses: actions/setup-python@v2
      with:
        python-version: '3.x'
    - name: Fetch framework mkdocs-base.yaml
      run: |
        FRAMEWORK_VERSION=$(grep -oP ':-\\K[^}"]+' .devcontainer/util/source_framework.sh | head -1)
        echo "Using framework version: $FRAMEWORK_VERSION"
        curl -fsSL "https://raw.githubusercontent.com/dynatrace-wwse/codespaces-framework/${FRAMEWORK_VERSION}/mkdocs-base.yaml" -o mkdocs-base.yaml
    - name: MKDocs - install requirements, build, gh-deploy
      run: |
        pip install --break-system-packages -r docs/requirements/requirements-mkdocs.txt
        git fetch origin gh-pages:gh-pages
        mkdocs build
        mkdocs gh-deploy
"""


def run(args):
    repo_path = Path(args.repo_path).resolve()
    version = args.framework_version
    dry_run = args.dry_run

    if not repo_path.is_dir():
        print(f"x {repo_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    if not (repo_path / ".devcontainer").is_dir():
        print(f"x {repo_path} has no .devcontainer/ directory", file=sys.stderr)
        sys.exit(1)

    # ── Phase 1: Audit ──
    print(f"Auditing {repo_path.name}...\n")

    found_files = []
    found_dirs = []

    for f in CATEGORY_A_FILES:
        p = repo_path / f
        if p.exists():
            found_files.append(f)
            print(f"  found  {f}")

    for d in CATEGORY_A_DIRS:
        p = repo_path / d
        if p.is_dir():
            count = sum(1 for _ in p.rglob("*") if _.is_file())
            found_dirs.append((d, count))
            print(f"  found  {d}/ ({count} files)")

    for f in CATEGORY_B_FILES:
        p = repo_path / f
        if p.exists():
            print(f"  found  {f} (will be replaced with thin wrapper)")

    has_source_fw = (repo_path / ".devcontainer/util/source_framework.sh").exists()
    if has_source_fw:
        print(f"  exists .devcontainer/util/source_framework.sh")

    if not found_files and not found_dirs:
        print("\n  No Category A files found. Repo may already be migrated.")
        if not has_source_fw:
            print("  WARNING: source_framework.sh is also missing — not a framework repo?")
        return

    print(f"\n  {len(found_files)} files + {len(found_dirs)} directories to remove")
    print(f"  {len(CATEGORY_B_FILES)} files to replace with thin wrappers")

    if dry_run:
        print("\n  --dry-run: no changes made")
        return

    # ── Phase 2: Clean Category A files ──
    print(f"\nCleaning Category A files...")

    for f in found_files:
        p = repo_path / f
        p.unlink()
        print(f"  deleted {f}")

    for d, count in found_dirs:
        p = repo_path / d
        shutil.rmtree(p)
        print(f"  deleted {d}/ ({count} files)")

    # Clean empty parent dirs left behind
    for d in [".devcontainer/runlocal"]:
        p = repo_path / d
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()
            print(f"  removed empty {d}/")

    # ── Phase 3: Replace Category B files with thin wrappers ──
    print(f"\nInstalling thin Makefile...")
    makefile_path = repo_path / ".devcontainer/Makefile"
    makefile_path.write_text(THIN_MAKEFILE)
    print(f"  replaced .devcontainer/Makefile (bootstrap + delegate to cache)")

    # ── Phase 4: Install source_framework.sh ──
    sf_path = repo_path / ".devcontainer/util/source_framework.sh"
    if not has_source_fw:
        print(f"\nInstalling source_framework.sh (v{version})...")
        sf_path.parent.mkdir(parents=True, exist_ok=True)
        sf_path.write_text(SOURCE_FRAMEWORK_TEMPLATE % version)
        print(f"  created .devcontainer/util/source_framework.sh")
    else:
        print(f"\n  source_framework.sh already exists, skipping install")

    # ── Phase 5: Migrate mkdocs ──
    mkdocs_path = repo_path / "mkdocs.yaml"
    if mkdocs_path.exists():
        content = mkdocs_path.read_text()
        if "INHERIT:" not in content:
            print(f"\n  MkDocs needs INHERIT migration — run:")
            print(
                f"  PYTHONPATH=. python3 -m sync.cli migrate-mkdocs "
                f"--repo dynatrace-wwse/{repo_path.name} --dry-run"
            )
        else:
            print(f"\n  mkdocs.yaml already uses INHERIT")

    # ── Phase 6: Update overrides/main.html ──
    overrides_path = repo_path / "docs/overrides/main.html"
    if overrides_path.exists():
        content = overrides_path.read_text()
        if "config.extra.rum_snippet" not in content:
            overrides_path.write_text(OVERRIDES_MAIN_HTML)
            print(f"  updated docs/overrides/main.html (parameterized RUM)")

    # ── Phase 7: Update deploy-ghpages.yaml ──
    ghpages_path = repo_path / ".github/workflows/deploy-ghpages.yaml"
    if ghpages_path.exists():
        content = ghpages_path.read_text()
        if "Fetch framework mkdocs-base.yaml" not in content:
            ghpages_path.write_text(DEPLOY_GHPAGES_TEMPLATE)
            print(f"  updated .github/workflows/deploy-ghpages.yaml")

    # ── Phase 8: Update .gitignore ──
    gitignore_path = repo_path / ".gitignore"
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        additions = []
        if ".devcontainer/.cache/" not in content:
            additions.append("\n# Framework cache\n.devcontainer/.cache/")
        if "mkdocs-base.yaml" not in content:
            additions.append(
                "\n# Framework base config fetched at runtime\nmkdocs-base.yaml"
            )
        if additions:
            with open(gitignore_path, "a") as f:
                f.write("\n" + "\n".join(additions) + "\n")
            print(f"  updated .gitignore")

    print(f"\nMigration complete. Review changes with 'git diff' then commit.")
