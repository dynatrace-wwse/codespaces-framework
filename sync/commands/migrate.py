"""sync migrate — Migrate a framework-based repo to the versioned pull model.

Operates locally on a cloned repo. Always targets repos listed in repos.yaml,
never the current directory. The --repo flag is required.

Image tiers control which framework files are relevant to each repo:
  minimal — No K8s, no Kind cluster. Just the core framework (util, p10k, runlocal).
  k8s     — Full K8s stack: Kind cluster, entrypoint, Dynakube yaml templates.
  ai      — Everything in k8s + AI-specific files (future).
"""

import json
import re
import shutil
import sys
from pathlib import Path

from sync.core.repos import VALID_IMAGE_TIERS, load_repos

# ── Tier-aware Category A files ──
# Framework-owned, pulled from cache at runtime. REMOVED from repos during migration.

# Universal — every tier gets these removed
CATEGORY_A_FILES = [
    ".devcontainer/util/functions.sh",
    ".devcontainer/util/variables.sh",
    ".devcontainer/util/greeting.sh",
    ".devcontainer/util/test_functions.sh",
    ".devcontainer/util/.count",
    ".devcontainer/makefile.sh",
    ".devcontainer/runlocal/helper.sh",
    ".devcontainer/Dockerfile",
    ".devcontainer/entrypoint.sh",
    # test_functions.sh is framework-owned; integration.sh is repo-specific (stays)
    ".devcontainer/test/test_functions.sh",
    # Legacy location — kind-cluster.yml moved to yaml/kind/ in framework
    ".devcontainer/kind-cluster.yml",
]

CATEGORY_A_DIRS = [
    ".devcontainer/apps",
    ".devcontainer/p10k",
    ".devcontainer/yaml",
]

# Category B files — framework-owned, stay in each repo as thin wrappers.
# These are REPLACED with framework templates during migration.
CATEGORY_B_FILES = [
    ".devcontainer/Makefile",
]

# Files that MUST remain in each repo (custom per repo, never removed)
REPO_CUSTOM_FILES = [
    ".devcontainer/devcontainer.json",
    ".devcontainer/post-create.sh",
    ".devcontainer/post-start.sh",
    ".devcontainer/util/source_framework.sh",
    ".devcontainer/util/my_functions.sh",
    ".devcontainer/test/integration.sh",
]


def _get_category_a(image_tier: str) -> tuple[list[str], list[str]]:
    """Return (files, dirs) for Category A based on image tier.

    Currently all tiers share the same Category A set. The tier parameter
    is kept for future extensibility (e.g. AI-specific files).
    """
    return list(CATEGORY_A_FILES), list(CATEGORY_A_DIRS)


# ── devcontainer.json validation ──
# Reference values that every repo must match for all 3 instantiation modes
# (Codespaces, VS Code DevContainer, local Docker).

DEVCONTAINER_CHECKS = {
    "image": {
        "expected": "shinojosa/dt-enablement:v1.2",
        "why": "Must reference the pre-built framework image (Docker Hub, multi-arch)",
    },
    "overrideCommand": {
        "expected": False,
        "why": "Must be false so the entrypoint.sh baked into the image runs",
    },
    "remoteUser": {
        "expected": "vscode",
        "why": "Base image user — required for file permissions across all instantiation modes",
    },
    "postCreateCommand": {
        "expected": "./.devcontainer/post-create.sh",
        "why": "Lifecycle hook for repo-specific setup",
    },
    "postStartCommand": {
        "expected": "./.devcontainer/post-start.sh",
        "why": "Lifecycle hook for repo-specific post-start actions",
    },
}

DEVCONTAINER_REQUIRED_RUNARGS = ["--init", "--privileged", "--network=host"]

DEVCONTAINER_REQUIRED_MOUNTS = [
    "source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"
]


def _strip_jsonc_comments(text: str) -> str:
    """Strip // and /* */ comments from JSONC text (devcontainer.json uses JSONC)."""
    # Remove block comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Remove line comments (but not inside strings)
    lines = []
    for line in text.split("\n"):
        in_string = False
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                in_string = not in_string
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/" and not in_string:
                line = line[:i]
                break
            i += 1
        lines.append(line)
    return "\n".join(lines)


def _parse_devcontainer(path: Path) -> dict | None:
    """Parse a devcontainer.json (JSONC) file. Returns None on failure."""
    try:
        raw = path.read_text()
        cleaned = _strip_jsonc_comments(raw)
        # Handle trailing commas (common in devcontainer.json)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        return json.loads(cleaned)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: could not parse devcontainer.json: {e}", file=sys.stderr)
        return None


def _validate_devcontainer(repo_path: Path) -> list[str]:
    """Validate devcontainer.json against framework reference. Returns list of issues."""
    dc_path = repo_path / ".devcontainer/devcontainer.json"
    if not dc_path.exists():
        return ["MISSING: .devcontainer/devcontainer.json does not exist"]

    data = _parse_devcontainer(dc_path)
    if data is None:
        return ["PARSE ERROR: could not parse devcontainer.json"]

    issues = []

    # Check key fields
    for field, check in DEVCONTAINER_CHECKS.items():
        actual = data.get(field)
        expected = check["expected"]
        if actual != expected:
            issues.append(
                f"  {field}: got {actual!r}, expected {expected!r}\n"
                f"    → {check['why']}"
            )

    # Check runArgs
    run_args = data.get("runArgs", [])
    for arg in DEVCONTAINER_REQUIRED_RUNARGS:
        if arg not in run_args:
            issues.append(f"  runArgs: missing {arg!r} — needed for local Docker instantiation")

    # Check mounts
    mounts = data.get("mounts", [])
    for mount in DEVCONTAINER_REQUIRED_MOUNTS:
        if mount not in mounts:
            issues.append(f"  mounts: missing {mount!r} — needed for Docker-in-Docker")

    # Check features is empty
    features = data.get("features", {})
    if features:
        issues.append(
            f"  features: should be empty {{}}, got {features!r}\n"
            f"    → Extensions/features break portability across instantiation modes"
        )

    # Check extensions is empty
    customizations = data.get("customizations", {})
    vscode = customizations.get("vscode", {})
    extensions = vscode.get("extensions", [])
    if extensions:
        issues.append(
            f"  extensions: should be empty [], got {extensions!r}\n"
            f"    → Extensions must be empty for portability"
        )

    return issues


def _resolve_repo_path(repo_name: str) -> Path:
    """Resolve a repo name to its local path (sibling to codespaces-framework)."""
    framework_dir = Path(__file__).parent.parent.parent  # sync/commands/ -> codespaces-framework
    return (framework_dir.parent / repo_name).resolve()


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
  git sparse-checkout set --no-cone \
    '.devcontainer/util/*' \
    '.devcontainer/p10k/*' \
    '/.devcontainer/test/test_functions.sh' \
    '.devcontainer/apps/*' \
    '/.devcontainer/Makefile' \
    '/.devcontainer/makefile.sh' \
    '.devcontainer/runlocal/*' \
    '/.devcontainer/Dockerfile' \
    '/.devcontainer/entrypoint.sh' \
    '.devcontainer/yaml/*' && \
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

# Repo name and root path (CURDIR = repo/.devcontainer where this Makefile lives)
REPO_NAME := $(notdir $(patsubst %/,%,$(dir $(CURDIR))))
REPO_ROOT := $(patsubst %/,%,$(dir $(CURDIR)))

# Bootstrap: populate host cache if missing
$(CACHE)/.complete:
	@echo "Bootstrapping framework v$(FRAMEWORK_VERSION)..."
	@mkdir -p $(dir $(CACHE))
	@git clone --depth 1 --filter=blob:none --sparse \
		-b $(FRAMEWORK_VERSION) \
		https://github.com/dynatrace-wwse/codespaces-framework.git \
		$(CACHE) 2>/dev/null
	@cd $(CACHE) && git sparse-checkout set --no-cone \
		'.devcontainer/util/*' \
		'.devcontainer/p10k/*' \
		'.devcontainer/test/*' \
		'.devcontainer/apps/*' \
		'/.devcontainer/Makefile' \
		'/.devcontainer/makefile.sh' \
		'.devcontainer/runlocal/*' \
		'/.devcontainer/Dockerfile' \
		'/.devcontainer/entrypoint.sh' \
		'.devcontainer/yaml/*'
	@touch $(CACHE)/.complete
	@# Write a wrapper that sources makefile.sh safely in cache mode:
	@# - strips top-level calls to getRepositoryName/getDockerEnvsFromEnvFile
	@# - sets ENV_FILE and RepositoryName to the repo's paths
	@# - re-sources helper.sh and calls getDockerEnvsFromEnvFile
	@printf '#!/bin/bash\n\
export ENV_FILE="$(CURDIR)/.env"\n\
export RepositoryName="$(REPO_NAME)"\n\
_MAKEFILE_DIR="$$(cd "$$(dirname "$${BASH_SOURCE[0]}")" && pwd)"\n\
eval "$$(sed "/^getRepositoryName$$/d; /^getDockerEnvsFromEnvFile$$/d" "$${_MAKEFILE_DIR}/makefile.sh")"\n\
export ENV_FILE="$(CURDIR)/.env"\n\
export RepositoryName="$(REPO_NAME)"\n\
VOLUMEMOUNTS="-v /var/run/docker.sock:/var/run/docker.sock -v /lib/modules:/lib/modules -v $(REPO_ROOT):/workspaces/$(REPO_NAME)"\n\
WORKINGDIR="-w /workspaces/$(REPO_NAME)"\n\
source "$${_MAKEFILE_DIR}/runlocal/helper.sh"\n\
getDockerEnvsFromEnvFile\n' > $(CACHE)/.devcontainer/cached_makefile.sh
	@echo "Framework v$(FRAMEWORK_VERSION) cached."

start: $(CACHE)/.complete
	@cd $(CACHE)/.devcontainer && bash -c 'source cached_makefile.sh; start'

build: $(CACHE)/.complete
	@cd $(CACHE)/.devcontainer && bash -c 'source cached_makefile.sh; build'

build-nocache: $(CACHE)/.complete
	@cd $(CACHE)/.devcontainer && bash -c 'source cached_makefile.sh; buildNoCache'

buildx: $(CACHE)/.complete
	@cd $(CACHE)/.devcontainer && bash -c 'source cached_makefile.sh; buildx'

integration: $(CACHE)/.complete
	@cd $(CACHE)/.devcontainer && bash -c 'source cached_makefile.sh; integration'

clean-cache:
	@rm -rf .cache/dt-framework
	@echo "Framework cache cleared."

clean-start: clean-cache
	@docker kill $$(docker ps -q) 2>/dev/null; docker rm $$(docker ps -aq) 2>/dev/null; true
	@echo "Containers removed."
	@$(MAKE) start
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


def _migrate_repo(entry, repo_path: Path, version: str, dry_run: bool) -> str:
    """Migrate a single repo. Returns status: 'migrated', 'up-to-date', 'skipped', or 'error'."""
    image_tier = entry.image_tier
    cat_a_files, cat_a_dirs = _get_category_a(image_tier)

    # ── Phase 1: Audit ──
    found_files = []
    found_dirs = []

    for f in cat_a_files:
        p = repo_path / f
        if p.exists():
            found_files.append(f)
            print(f"    found  {f}")

    for d in cat_a_dirs:
        p = repo_path / d
        if p.is_dir():
            count = sum(1 for _ in p.rglob("*") if _.is_file())
            found_dirs.append((d, count))
            print(f"    found  {d}/ ({count} files)")

    for f in CATEGORY_B_FILES:
        p = repo_path / f
        if p.exists():
            print(f"    found  {f} (will be replaced with thin wrapper)")

    has_source_fw = (repo_path / ".devcontainer/util/source_framework.sh").exists()

    # ── Phase 1b: Validate devcontainer.json ──
    dc_issues = _validate_devcontainer(repo_path)
    if dc_issues:
        print(f"    devcontainer.json: {len(dc_issues)} issue(s)")
        for issue in dc_issues:
            print(f"      ❌ {issue}")
    else:
        print(f"    ✅ devcontainer.json valid")

    # Check if templates need updating
    sf_path = repo_path / ".devcontainer/util/source_framework.sh"
    makefile_path = repo_path / ".devcontainer/Makefile"
    templates_current = True
    if has_source_fw:
        content = sf_path.read_text()
        m = re.search(r'FRAMEWORK_VERSION="\$\{FRAMEWORK_VERSION:-([^}]+)\}"', content)
        pinned = m.group(1) if m else None
        if pinned:
            if content != SOURCE_FRAMEWORK_TEMPLATE % pinned:
                templates_current = False
        else:
            templates_current = False
    else:
        templates_current = False

    if makefile_path.exists() and makefile_path.read_text() != THIN_MAKEFILE:
        templates_current = False

    needs_work = bool(found_files) or bool(found_dirs) or not templates_current

    if not needs_work:
        print(f"    ✅ already migrated and up to date")
        return "up-to-date"

    if found_files or found_dirs:
        print(f"    {len(found_files)} files + {len(found_dirs)} directories to remove")
    if not templates_current:
        print(f"    templates need updating (Makefile, source_framework.sh)")

    if dry_run:
        return "needs-migration"

    # ── Phase 2: Clean Category A files ──
    if found_files or found_dirs:
        print(f"    Cleaning Category A files...")
        for f in found_files:
            (repo_path / f).unlink()
            print(f"      deleted {f}")
        for d, count in found_dirs:
            shutil.rmtree(repo_path / d)
            print(f"      deleted {d}/ ({count} files)")
        for d in [".devcontainer/runlocal", ".devcontainer/test"]:
            p = repo_path / d
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
                print(f"      removed empty {d}/")

    # ── Phase 3: Replace Category B files with thin wrappers ──
    makefile_path.parent.mkdir(parents=True, exist_ok=True)
    makefile_path.write_text(THIN_MAKEFILE)
    print(f"    updated Makefile")

    # ── Phase 4: Install/update source_framework.sh ──
    sf_path.parent.mkdir(parents=True, exist_ok=True)
    if has_source_fw:
        existing = sf_path.read_text()
        m = re.search(r'FRAMEWORK_VERSION="\$\{FRAMEWORK_VERSION:-([^}]+)\}"', existing)
        pinned_version = m.group(1) if m else version
        sf_path.write_text(SOURCE_FRAMEWORK_TEMPLATE % pinned_version)
        print(f"    updated source_framework.sh (version pin {pinned_version})")
    else:
        sf_path.write_text(SOURCE_FRAMEWORK_TEMPLATE % version)
        print(f"    created source_framework.sh (v{version})")

    # ── Phase 5: Migrate mkdocs ──
    mkdocs_path = repo_path / "mkdocs.yaml"
    if mkdocs_path.exists():
        content = mkdocs_path.read_text()
        if "INHERIT:" not in content:
            try:
                import yaml as _yaml

                # Material theme uses !!python/name: tags — handle gracefully
                class _SafeLoader(_yaml.SafeLoader):
                    pass
                _SafeLoader.add_multi_constructor(
                    "tag:yaml.org,2002:python/",
                    lambda loader, suffix, node: f"!!python/{suffix}{loader.construct_scalar(node)}",
                )
                config = _yaml.load(content, Loader=_SafeLoader)
                lines = ["INHERIT: mkdocs-base.yaml", ""]
                for field in ("site_name", "repo_name", "repo_url"):
                    val = config.get(field, "")
                    if val:
                        lines.append(f'{field}: "{val}"')
                nav = config.get("nav", [])
                if nav:
                    lines.append("nav:")
                    for item in nav:
                        if isinstance(item, dict):
                            for k, v in item.items():
                                lines.append(f'  - "{k}": {v}')
                        else:
                            lines.append(f"  - {item}")
                extra = config.get("extra", {})
                rum_snippet = extra.get("rum_snippet", "")
                if rum_snippet:
                    lines.append("")
                    lines.append("extra:")
                    lines.append(f'  rum_snippet: "{rum_snippet}"')
                mkdocs_path.write_text("\n".join(lines) + "\n")
                print(f"    migrated mkdocs.yaml to INHERIT pattern")
            except Exception as e:
                print(f"    ❌ mkdocs migration failed: {e}")

    # ── Phase 6: Update overrides/main.html + extract RUM snippet ──
    overrides_path = repo_path / "docs/overrides/main.html"
    if overrides_path.exists():
        content = overrides_path.read_text()
        if "config.extra.rum_snippet" not in content:
            # Extract existing RUM snippet URL before replacing (last match = the real one, not commented placeholder)
            rum_url = ""
            rum_matches = re.findall(
                r'src="(https://js-cdn\.dynatrace\.com/jstag/[^"]+)"',
                content,
            )
            if rum_matches:
                rum_url = rum_matches[-1]  # last match is the active one

            overrides_path.write_text(OVERRIDES_MAIN_HTML)
            print(f"    updated docs/overrides/main.html")

            # Add rum_snippet to mkdocs.yaml if extracted
            if rum_url and mkdocs_path.exists():
                mk_content = mkdocs_path.read_text()
                if "rum_snippet" not in mk_content:
                    if "extra:" not in mk_content:
                        mk_content += f'\nextra:\n  rum_snippet: "{rum_url}"\n'
                    else:
                        mk_content = mk_content.replace(
                            "extra:",
                            f'extra:\n  rum_snippet: "{rum_url}"',
                        )
                    mkdocs_path.write_text(mk_content)
                    print(f"    extracted RUM snippet to mkdocs.yaml")

    # ── Phase 7: Update deploy-ghpages.yaml ──
    ghpages_path = repo_path / ".github/workflows/deploy-ghpages.yaml"
    if ghpages_path.exists():
        content = ghpages_path.read_text()
        if "Fetch framework mkdocs-base.yaml" not in content:
            ghpages_path.write_text(DEPLOY_GHPAGES_TEMPLATE)
            print(f"    updated deploy-ghpages.yaml")

    # ── Phase 8: Update .gitignore ──
    gitignore_path = repo_path / ".gitignore"
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        additions = []
        if ".devcontainer/.cache/" not in content:
            additions.append("\n# Framework cache\n.devcontainer/.cache/")
        if "mkdocs-base.yaml" not in content:
            additions.append("\n# Framework base config fetched at runtime\nmkdocs-base.yaml")
        if "Dockerfile.framework" not in content:
            additions.append("\n# Temporary Dockerfile from cache for local builds\nDockerfile.framework")
        if additions:
            with open(gitignore_path, "a") as f:
                f.write("\n" + "\n".join(additions) + "\n")
            print(f"    updated .gitignore")

    # ── Phase 9: Migrate .env location ──
    _migrate_env_location(repo_path)

    return "migrated"


def _migrate_env_location(repo_path: Path):
    """Move .env from runlocal/ to .devcontainer/ and update all references."""
    devcontainer = repo_path / ".devcontainer"

    # Move .env files if they exist at the old location
    runlocal = devcontainer / "runlocal"
    if runlocal.is_dir():
        for env_file in runlocal.glob(".env*"):
            dest = devcontainer / env_file.name
            if not dest.exists():
                env_file.rename(dest)
                print(f"    moved runlocal/{env_file.name} → .devcontainer/{env_file.name}")

    # Update .vscode/mcp.json
    mcp_path = repo_path / ".vscode/mcp.json"
    if mcp_path.exists():
        content = mcp_path.read_text()
        if "runlocal/.env" in content:
            content = content.replace(
                ".devcontainer/runlocal/.env",
                ".devcontainer/.env",
            )
            mcp_path.write_text(content)
            print(f"    updated .vscode/mcp.json")

    # Update .gitignore
    gitignore_path = repo_path / ".gitignore"
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        if ".devcontainer/runlocal/.env" in content:
            content = content.replace(
                ".devcontainer/runlocal/.env",
                ".devcontainer/.env",
            )
            gitignore_path.write_text(content)
            print(f"    updated .gitignore (.env path)")

    # Update devcontainer.json comment
    dc_path = devcontainer / "devcontainer.json"
    if dc_path.exists():
        content = dc_path.read_text()
        if "runlocal/.env" in content:
            content = content.replace("runlocal/.env", ".env")
            dc_path.write_text(content)
            print(f"    updated devcontainer.json (.env comment)")

    # Update CI workflow
    for wf_name in ["integration-tests.yaml", "integration-tests-reusable.yaml"]:
        wf_path = repo_path / ".github/workflows" / wf_name
        if wf_path.exists():
            content = wf_path.read_text()
            if "runlocal/.env" in content:
                content = content.replace(
                    "runlocal/.env",
                    ".env",
                ).replace(
                    ".devcontainer/runlocal/.env",
                    ".devcontainer/.env",
                )
                wf_path.write_text(content)
                print(f"    updated {wf_name}")

    # Update docs referencing old .env path
    docs_dir = repo_path / "docs"
    if docs_dir.is_dir():
        for md_file in docs_dir.rglob("*.md"):
            content = md_file.read_text()
            if "runlocal/.env" in content:
                content = content.replace(
                    ".devcontainer/runlocal/.env",
                    ".devcontainer/.env",
                ).replace(
                    "runlocal/.env",
                    ".devcontainer/.env",
                )
                md_file.write_text(content)
                print(f"    updated {md_file.relative_to(repo_path)}")


def run(args):
    version = args.framework_version
    dry_run = args.dry_run
    target_repo = getattr(args, "repo", None)

    from sync.core.repos import filter_sync_targets
    repos = load_repos()

    # Filter to specific repo or all sync-managed
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"x '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = filter_sync_targets(repos)

    print(f"{'[DRY RUN] ' if dry_run else ''}Migrating {len(repos)} repos\n")

    counts = {"migrated": 0, "up-to-date": 0, "needs-migration": 0, "skipped": 0, "error": 0}

    for entry in repos:
        repo_path = _resolve_repo_path(entry.repo_name)

        print(f"── {entry.repo} ──")
        print(f"  path: {repo_path}")
        print(f"  image_tier: {entry.image_tier}")

        if not repo_path.is_dir():
            print(f"  📭 local clone not found")
            counts["skipped"] += 1
            print()
            continue

        if not (repo_path / ".devcontainer").is_dir():
            print(f"  📭 no .devcontainer/ directory")
            counts["skipped"] += 1
            print()
            continue

        try:
            status = _migrate_repo(entry, repo_path, version, dry_run)
            counts[status] += 1
        except Exception as e:
            print(f"  ❌ error: {e}")
            counts["error"] += 1

        print()

    # Summary
    if dry_run:
        print(f"Summary: {counts['needs-migration']} need migration, "
              f"{counts['up-to-date']} up to date, {counts['skipped']} skipped")
    else:
        print(f"Summary: {counts['migrated']} migrated, "
              f"{counts['up-to-date']} up to date, {counts['skipped']} skipped, "
              f"{counts['error']} errors")
