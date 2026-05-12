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


DEVCONTAINER_JSON_TEMPLATE = r"""{
  "name": "Dynatrace Enablement Container",
  // Pulling the image from the Dockerhub, runs on AMD64 and ARM64. Pulling is normally faster.
  "image":"shinojosa/dt-enablement:v1.2",
  /*
  // Building the image from the Dockerfile
  "build": {
    "dockerfile": "Dockerfile"
    },
  */
  // When running locally we pass the 'secrets' as env variables via runArgs "--env-file",".devcontainer/.env"
  "runArgs": ["--init", "--privileged", "--network=host"],
  "mounts": ["source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"],
  // Entrypoint and CMD are overwritten by VSCode
  "overrideCommand": false,

  // some base images require a specific user name.
  "remoteUser": "vscode",

  "postCreateCommand": "./.devcontainer/post-create.sh",

  "postStartCommand": "./.devcontainer/post-start.sh",

  "features": {},
  "customizations": {
    "vscode": {
      // Set container specific settings
      "settings": {
        "terminal.integrated.defaultProfile.linux": "zsh"
      },
      "extensions": [ ]
    }
  },
  // Use 'forwardPorts' to make a list of ports inside the container available locally.
  "forwardPorts": [
    30100
  ],
  // add labels
  "portsAttributes": {
    "30100": { "label": "Application Web UI" }
  },
  // minimal CPU when running DT components and apps.
  "hostRequirements": {
    "cpus": 4
  },
  "secrets": {
    "DT_ENVIRONMENT": {
      "description": "URL to your Dynatrace Platform eg. https://abc123.apps.dynatrace.com or for sprint -> https://abc123.sprint.apps.dynatracelabs.com"
    },
    "DT_OPERATOR_TOKEN": {
      "description": "Dynatrace Operator Token"
    },
    "DT_INGEST_TOKEN": {
      "description": "Dynatrace Ingest Token"
    }
  }
}
"""

INTEGRATION_TESTS_TEMPLATE = """\
name: integration-tests
run-name: ${{ github.event.head_commit.message }} - PR integration test
permissions:
  contents: write
on:
  pull_request:
    branches:
      - main
jobs:
  codespaces-integration-test-with-dynatrace-deployment:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    env:
      DT_ENVIRONMENT: ${{ secrets.DT_ENVIRONMENT }}
      DT_OPERATOR_TOKEN: ${{ secrets.DT_OPERATOR_TOKEN }}
      DT_INGEST_TOKEN: ${{ secrets.DT_INGEST_TOKEN }}
    steps:
      - name: Commit info
        run: |
          echo "Commit SHA: ${{ github.sha }}"
          echo "Triggered by: ${{ github.actor }}"
          echo "Repository ${{ github.repository }}"
          echo "Commit message ${{ github.event.head_commit.message }}"
          echo "Event ${{ github.event_name }}"
      - name: Check out repository code
        uses: actions/checkout@v4
      - name: Add secrets in .env file
        run: |
            cd .devcontainer
            cat <<EOF >> .env
            DT_ENVIRONMENT=$DT_ENVIRONMENT
            DT_OPERATOR_TOKEN=$DT_OPERATOR_TOKEN
            DT_INGEST_TOKEN=$DT_INGEST_TOKEN
            EOF
            sed -i '/"runArgs": \\["--init", "--privileged", "--network=host"\\]/c\\  "runArgs": ["--init", "--privileged", "--network=host", "--env-file",".devcontainer/.env"]' devcontainer.json
      - name: Test Codespace (devcontainer) build and run commands
        uses: devcontainers/ci@v0.3
        with:
          imageName: shinojosa/dt-enablement
          cacheFrom: shinojosa/dt-enablement
          push: never
          runCmd: |
            zsh .devcontainer/test/integration.sh
"""

INTEGRATION_SH_TEMPLATE = """\
#!/bin/bash
# Load framework
source .devcontainer/util/source_framework.sh

printInfoSection "Running integration Tests for $RepositoryName"

assertRunningPod dynatrace operator

assertRunningPod dynatrace activegate

# Call assertRunningApp with your app's registered name, e.g.:
# assertRunningApp myapp
"""

MY_FUNCTIONS_TEMPLATE = """\
#!/bin/bash
# Custom functions for this repository
# Define your own functions here and call them from post-create.sh
"""

POST_CREATE_TEMPLATE = """\
#!/bin/bash
#loading functions to script
export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal

startKindCluster

installK9s

# Dynatrace Operator is deployed automatically, secrets are read from the env.
dynatraceDeployOperator

# You can deploy CNFS or AppOnly
deployCloudNative
#deployApplicationMonitoring

# If you want to deploy your own App, just create a function in my_functions.sh and call it here.

# This step is needed, do not remove it
finalizePostCreation

printInfoSection "Your dev container finished creating"
"""

POST_START_TEMPLATE = """\
#!/bin/bash
##############################################################
##  In here you add whatever action should happen after the container has been started
##############################################################
#Load the functions into the shell
source .devcontainer/util/source_framework.sh

printInfoSection "Your dev.container finished starting up"
"""

MCP_JSON_TEMPLATE = """\
{
	"servers": {
		"dynatrace-mcp-server": {
			"type": "stdio",
			"command": "npx",
			"cwd": "${workspaceFolder}",
			"args": ["-y","@dynatrace-oss/dynatrace-mcp-server@latest"],
			"envFile": "${workspaceFolder}/.devcontainer/.env"
		}
	}
}
"""

GITIGNORE_TEMPLATE = """\
# Framework cache
.devcontainer/.cache/

# Framework runtime files (generated at container start)
.devcontainer/util/.count*

# Framework base config fetched at runtime
mkdocs-base.yaml

# no env files
.devcontainer/.env*

# mkdocs renders static files in site
site/*

# No generated file
**/gen/*/*.yaml
**/gen/**.yaml
**-gen.yaml

# no logs
*.log

# Node
node_modules

# macOS
.DS_Store

# IDE
.vscode/*
!.vscode/mcp.json
.idea

# Python
__pycache__/
"""


def _scaffold_repo(entry, repo_path: Path, version: str):
    """Add missing framework files to a repo. Creates files that don't exist."""
    owner = entry.owner
    name = entry.repo_name
    repo = entry.repo
    created = []

    # devcontainer.json
    dc_path = repo_path / ".devcontainer/devcontainer.json"
    if not dc_path.exists():
        dc_path.parent.mkdir(parents=True, exist_ok=True)
        dc_path.write_text(DEVCONTAINER_JSON_TEMPLATE)
        created.append("devcontainer.json")
    else:
        # Fix old-style devcontainer.json: dockerFile → image, wrong mount target
        content = dc_path.read_text()
        changed = False
        if '"dockerFile"' in content or '"dockerfile"' in content:
            # Replace build with image pull
            content = re.sub(
                r'"dockerFile"\s*:\s*"Dockerfile"',
                '"image": "shinojosa/dt-enablement:v1.2"',
                content, flags=re.IGNORECASE,
            )
            changed = True
        if "docker-host.sock" in content:
            content = content.replace("docker-host.sock", "docker.sock")
            changed = True
        if 'chmod +x .devcontainer/post-create.sh && .devcontainer/post-create.sh' in content:
            content = content.replace(
                'chmod +x .devcontainer/post-create.sh && .devcontainer/post-create.sh',
                './.devcontainer/post-create.sh',
            )
            changed = True
        if 'chmod +x .devcontainer/post-start.sh && .devcontainer/post-start.sh' in content:
            content = content.replace(
                'chmod +x .devcontainer/post-start.sh && .devcontainer/post-start.sh',
                './.devcontainer/post-start.sh',
            )
            changed = True
        if changed:
            dc_path.write_text(content)
            created.append("devcontainer.json (fixed)")

    # post-create.sh
    pc_path = repo_path / ".devcontainer/post-create.sh"
    if not pc_path.exists():
        pc_path.write_text(POST_CREATE_TEMPLATE)
        created.append("post-create.sh")

    # post-start.sh
    ps_path = repo_path / ".devcontainer/post-start.sh"
    if not ps_path.exists():
        ps_path.write_text(POST_START_TEMPLATE)
        created.append("post-start.sh")

    # my_functions.sh
    mf_path = repo_path / ".devcontainer/util/my_functions.sh"
    if not mf_path.exists():
        mf_path.parent.mkdir(parents=True, exist_ok=True)
        mf_path.write_text(MY_FUNCTIONS_TEMPLATE)
        created.append("util/my_functions.sh")

    # test/integration.sh
    ti_path = repo_path / ".devcontainer/test/integration.sh"
    if not ti_path.exists():
        ti_path.parent.mkdir(parents=True, exist_ok=True)
        ti_path.write_text(INTEGRATION_SH_TEMPLATE)
        created.append("test/integration.sh")

    # .github/workflows/integration-tests.yaml
    it_path = repo_path / ".github/workflows/integration-tests.yaml"
    if not it_path.exists():
        it_path.parent.mkdir(parents=True, exist_ok=True)
        it_path.write_text(INTEGRATION_TESTS_TEMPLATE)
        created.append(".github/workflows/integration-tests.yaml")

    # .github/workflows/deploy-ghpages.yaml
    dg_path = repo_path / ".github/workflows/deploy-ghpages.yaml"
    if not dg_path.exists():
        dg_path.parent.mkdir(parents=True, exist_ok=True)
        dg_path.write_text(DEPLOY_GHPAGES_TEMPLATE)
        created.append(".github/workflows/deploy-ghpages.yaml")

    # .vscode/mcp.json
    mcp_path = repo_path / ".vscode/mcp.json"
    if not mcp_path.exists():
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(MCP_JSON_TEMPLATE)
        created.append(".vscode/mcp.json")

    # .gitignore
    gi_path = repo_path / ".gitignore"
    if not gi_path.exists():
        gi_path.write_text(GITIGNORE_TEMPLATE)
        created.append(".gitignore")

    # docs/ directory with minimal structure
    docs_path = repo_path / "docs"
    if not docs_path.exists():
        docs_path.mkdir(parents=True, exist_ok=True)
        (docs_path / "index.md").write_text(f"# {name}\n\nWelcome to the {name} lab.\n")
        # Also need requirements for mkdocs
        req_path = docs_path / "requirements"
        req_path.mkdir(parents=True, exist_ok=True)
        (req_path / "requirements-mkdocs.txt").write_text(
            "mkdocs-material\npymdown-extensions\n"
        )
        # overrides for RUM
        overrides_path = docs_path / "overrides"
        overrides_path.mkdir(parents=True, exist_ok=True)
        (overrides_path / "main.html").write_text(OVERRIDES_MAIN_HTML)
        created.append("docs/ (index.md, requirements, overrides)")

    # mkdocs.yaml
    mk_path = repo_path / "mkdocs.yaml"
    if not mk_path.exists():
        title = name.replace("-", " ").replace("enablement ", "").title()
        mk_path.write_text(
            f'INHERIT: mkdocs-base.yaml\n\n'
            f'site_name: "Dynatrace Enablement Lab: {title}"\n'
            f'repo_name: "View Code on GitHub"\n'
            f'repo_url: "https://github.com/{repo}"\n'
            f'nav:\n'
            f"  - \"1. About\": index.md\n"
        )
        created.append("mkdocs.yaml")

    # LICENSE
    license_path = repo_path / "LICENSE"
    if not license_path.exists():
        # Copy from framework
        fw_license = Path(__file__).parent.parent.parent / "LICENSE"
        if fw_license.exists():
            shutil.copy2(fw_license, license_path)
            created.append("LICENSE")

    if created:
        print(f"    📦 scaffolded: {', '.join(created)}")
    else:
        print(f"    ✅ all framework files present")


def _migrate_repo(entry, repo_path: Path, version: str, dry_run: bool) -> str:
    """Migrate a single repo. Returns status: 'migrated', 'up-to-date', 'skipped', or 'error'."""

    # ── Phase 0: Scaffold missing framework files ──
    if not dry_run:
        _scaffold_repo(entry, repo_path, version)

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

    # ── Phase 1b: Fix devcontainer.json image version + validate ──
    dc_path = repo_path / ".devcontainer/devcontainer.json"
    if dc_path.exists():
        dc_raw = dc_path.read_text()
        # Auto-fix old image versions (v1.0, v1.1 → v1.2)
        if '"shinojosa/dt-enablement:v1.1"' in dc_raw or '"shinojosa/dt-enablement:v1.0"' in dc_raw:
            dc_raw = dc_raw.replace(
                '"shinojosa/dt-enablement:v1.1"', '"shinojosa/dt-enablement:v1.2"'
            ).replace(
                '"shinojosa/dt-enablement:v1.0"', '"shinojosa/dt-enablement:v1.2"'
            )
            dc_path.write_text(dc_raw)
            print(f"    🔄 devcontainer.json image bumped to v1.2")

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

    if found_files or found_dirs:
        print(f"    {len(found_files)} files + {len(found_dirs)} directories to remove")
    if not templates_current:
        print(f"    templates need updating (Makefile, source_framework.sh)")

    if dry_run:
        if needs_work:
            return "needs-migration"
        print(f"    ✅ core migration up to date")
        return "up-to-date"

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

    # ── Phase 3: Replace Category B files with thin wrappers (only if outdated) ──
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
        if ".count" not in content:
            additions.append("\n# Framework runtime files\n.devcontainer/util/.count*")
        if "mkdocs-base.yaml" not in content:
            additions.append("\n# Framework base config fetched at runtime\nmkdocs-base.yaml")
        if "Dockerfile.framework" not in content:
            additions.append("\n# Temporary Dockerfile from cache for local builds\nDockerfile.framework")
        if additions:
            with open(gitignore_path, "a") as f:
                f.write("\n" + "\n".join(additions) + "\n")
            print(f"    updated .gitignore")

    # Untrack .count if it's tracked by git
    count_file = repo_path / ".devcontainer/util/.count"
    if count_file.exists():
        import subprocess
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".devcontainer/util/.count"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            subprocess.run(
                ["git", "rm", "--cached", ".devcontainer/util/.count"],
                cwd=repo_path, capture_output=True, text=True,
            )
            print(f"    untracked .devcontainer/util/.count")

    # ── Phase 9: Migrate .env location ──
    _migrate_env_location(repo_path)

    # ── Phase 10: Validate and fix README badges ──
    _validate_readme(entry, repo_path)

    return "migrated"


README_BADGES_TEMPLATE = """\
<!-- markdownlint-disable-next-line -->
# <img src="https://cdn.bfldr.com/B686QPH3/at/w5hnjzb32k5wcrcxnwcx4ckg/Dynatrace_signet_RGB_HTML.svg?auto=webp&format=pngg" alt="DT logo" width="30"> {title}

[![Dynatrace](https://img.shields.io/badge/Dynatrace-Intelligence-purple?logo=dynatrace&logoColor=white)](https://dynatrace-wwse.github.io/codespaces-framework/dynatrace-integration/#mcp-server-integration)
[![Mastering](https://img.shields.io/badge/Mastering-Complexity-8A2BE2?logo=dynatrace)](https://dynatrace-wwse.github.io)
[![Downloads](https://img.shields.io/docker/pulls/shinojosa/dt-enablement?logo=docker)](https://hub.docker.com/r/shinojosa/dt-enablement)
[![Integration tests](https://github.com/{repo}/actions/workflows/integration-tests.yaml/badge.svg)](https://github.com/{repo}/actions)
[![Version](https://img.shields.io/github/v/release/{repo}?color=blueviolet)](https://github.com/{repo}/releases)
[![Commits](https://img.shields.io/github/commits-since/{repo}/latest?color=ff69b4&include_prereleases)](https://github.com/{repo}/graphs/commit-activity)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/{repo}/blob/main/LICENSE)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-green)](https://{owner}.github.io/{name}/)
"""

README_FOOTER_TEMPLATE = """\
## [📖 View the Lab Guide](https://{owner}.github.io/{name}/)
"""


OLD_BADGES = {
    "Davis%20CoPilot-AI%20Powered": "Dynatrace-Intelligence",
    "Powered_by-DT_Enablement": "Mastering-Complexity",
    "[Davis CoPilot]": "[Dynatrace]",
    "[dt-badge]": "[Mastering]",
    # Fix Mastering badge link (was pointing to framework, now points to org page)
    "badge/Mastering-Complexity-8A2BE2?logo=dynatrace)](https://dynatrace-wwse.github.io/codespaces-framework/)":
        "badge/Mastering-Complexity-8A2BE2?logo=dynatrace)](https://dynatrace-wwse.github.io)",
}


def _validate_readme(entry, repo_path: Path):
    """Validate README.md badges and footer link reference the correct repo."""
    readme_path = repo_path / "README.md"

    # Rename readme.md to README.md if needed
    readme_lower = repo_path / "readme.md"
    if not readme_path.exists() and readme_lower.exists():
        readme_lower.rename(readme_path)
        print(f"    🔄 renamed readme.md → README.md")

    if not readme_path.exists():
        print(f"    ⚠️  README.md missing")
        return

    content = readme_path.read_text()
    owner = entry.owner
    name = entry.repo_name
    repo = entry.repo
    changed = False
    issues = []

    # Migrate old badges to new ones
    for old, new in OLD_BADGES.items():
        if old in content:
            content = content.replace(old, new)
            changed = True

    # Fix integration tests badge: plain image → linked to /actions
    # ![Integration tests](badge.svg) → [![Integration tests](badge.svg)](repo/actions)
    unlinked_badge = re.search(
        r'(?<!\[)!\[Integration tests\]\((https://github\.com/[^)]+/badge\.svg)\)',
        content,
    )
    if unlinked_badge:
        old_badge = unlinked_badge.group(0)
        badge_svg = unlinked_badge.group(1)
        # Extract repo URL from badge SVG path
        actions_url = re.sub(r'/actions/workflows/.*', '/actions', badge_svg)
        new_badge = f"[![Integration tests]({badge_svg})]({actions_url})"
        content = content.replace(old_badge, new_badge)
        changed = True

    if changed:
        readme_path.write_text(content)
        print(f"    🔄 README.md badges updated")

    # Check and add missing badges
    expected_badges = [
        (f"github.com/{repo}/actions", f"[![Integration tests](https://github.com/{repo}/actions/workflows/integration-tests.yaml/badge.svg)](https://github.com/{repo}/actions)"),
        (f"github.com/{repo}/releases", f"[![Version](https://img.shields.io/github/v/release/{repo}?color=blueviolet)](https://github.com/{repo}/releases)"),
        (f"github.com/{repo}/graphs", f"[![Commits](https://img.shields.io/github/commits-since/{repo}/latest?color=ff69b4&include_prereleases)](https://github.com/{repo}/graphs/commit-activity)"),
        (f"github.com/{repo}/blob/main/LICENSE", f"[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/{repo}/blob/main/LICENSE)"),
        (f"{owner}.github.io/{name}", f"[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-green)](https://{owner}.github.io/{name}/)"),
    ]
    missing_badges = []
    for badge_url, badge_md in expected_badges:
        if badge_url not in content:
            missing_badges.append(badge_md)
            issues.append(f"missing badge: {badge_url}")

    # Insert missing badges after the first heading line
    if missing_badges:
        lines = content.split("\n")
        insert_idx = None
        for i, line in enumerate(lines):
            if line.startswith("#"):
                # Find the last consecutive badge line after heading
                j = i + 1
                while j < len(lines) and (lines[j].startswith("[![") or lines[j].startswith("![") or lines[j].strip() == ""):
                    j += 1
                insert_idx = j
                break
        if insert_idx is not None:
            for badge in missing_badges:
                lines.insert(insert_idx, badge)
                insert_idx += 1
            content = "\n".join(lines)
            readme_path.write_text(content)
            changed = True
            print(f"    📝 added {len(missing_badges)} missing badge(s)")

    # Check for wrong repo references in badges (common when copying from template)
    badge_repos = re.findall(r"github\.com/dynatrace-wwse/([\w-]+)/(?:actions|releases|graphs|blob)", content)
    for found_repo in badge_repos:
        if found_repo != name:
            issues.append(f"badge references wrong repo: {found_repo} (should be {name})")

    # Check footer link
    footer_links = re.findall(r"\[.*?\]\(https://[\w.-]+\.github\.io/([\w-]+)/?\)", content)
    if not footer_links:
        issues.append(f"missing footer link to GitHub Pages")
    else:
        for link_name in footer_links:
            if link_name != name and "codespaces-framework" not in link_name:
                issues.append(f"footer links to wrong repo: {link_name} (should be {name})")

    if issues:
        print(f"    ⚠️  README.md: {len(issues)} issue(s)")
        for issue in issues:
            print(f"      ⚠️  {issue}")
    else:
        print(f"    ✅ README.md badges and links valid")


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

        print(f"── {entry.url} ──")
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
