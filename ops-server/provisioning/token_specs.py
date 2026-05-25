"""Token spec definitions and per-repo loader.

Each repo can declare what API tokens it needs in .devcontainer/yaml/dt-tokens.yaml.
Falls back to the framework default (DEFAULT_SPECS) if no repo file exists.

Spec file format (YAML):
    tokens:
      - name_suffix: operator       # appended to the token name prefix
        env_var: DT_OPERATOR_TOKEN  # env var written to .devcontainer/.env
        scopes:
          - activeGateTokenManagement.write
          - DataExport
          - ...
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx
import yaml

log = logging.getLogger("ops-provisioning")

_GH_RAW = "https://raw.githubusercontent.com/{repo}/{ref}/.devcontainer/yaml/dt-tokens.yaml"
_FW_RAW = "https://raw.githubusercontent.com/dynatrace-wwse/codespaces-framework/main/.devcontainer/yaml/dt-tokens.yaml"


@dataclass
class TokenSpec:
    name_suffix: str    # e.g. "operator"
    env_var: str        # e.g. "DT_OPERATOR_TOKEN"
    scopes: list[str]


# Standard DT Operator + Ingest tokens — used by all K8s enablement repos.
DEFAULT_SPECS: list[TokenSpec] = [
    TokenSpec(
        name_suffix="operator",
        env_var="DT_OPERATOR_TOKEN",
        scopes=[
            "activeGateTokenManagement.write",
            "entities.read",
            "settings.read",
            "settings.write",
            "DataExport",
            "InstallerDownload",
        ],
    ),
    TokenSpec(
        name_suffix="ingest",
        env_var="DT_INGEST_TOKEN",
        scopes=[
            "metrics.ingest",
            "logs.ingest",
            "events.ingest",
            "openTelemetryTrace.ingest",
        ],
    ),
]


def _parse_yaml(content: str) -> list[TokenSpec]:
    data = yaml.safe_load(content)
    specs = []
    for t in data.get("tokens", []):
        specs.append(TokenSpec(
            name_suffix=t["name_suffix"],
            env_var=t["env_var"],
            scopes=t.get("scopes", []),
        ))
    return specs or DEFAULT_SPECS


async def load_token_specs(repo: str, ref: str = "main") -> list[TokenSpec]:
    """Fetch token spec for a repo from GitHub, fall back to framework default.

    Resolution order:
      1. {repo}/.devcontainer/yaml/dt-tokens.yaml  (repo-specific)
      2. codespaces-framework/.devcontainer/yaml/dt-tokens.yaml  (framework default)
      3. Hardcoded DEFAULT_SPECS  (offline fallback)
    """
    urls = [
        _GH_RAW.format(repo=repo, ref=ref),
        _FW_RAW,
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        for url in urls:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    specs = _parse_yaml(r.text)
                    log.info("Loaded token specs from %s (%d tokens)", url, len(specs))
                    return specs
            except Exception as exc:
                log.debug("Could not fetch %s: %s", url, exc)

    log.info("Using hardcoded DEFAULT_SPECS (no remote spec found)")
    return DEFAULT_SPECS
