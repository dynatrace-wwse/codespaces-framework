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


# ── gen3 scope translation ──────────────────────────────────────────────────────
# On tenants where classic apiToken creation is disabled, training tokens are minted as
# platform tokens (dt0s16). Classic apiToken scopes have no meaning there — map each to its
# platform-scope equivalent. A value of None = no platform equivalent: the capability is
# either not needed for applicationMonitoring (DataExport/InstallerDownload) or is covered by
# a pre-minted ActiveGate token (activeGateTokenManagement.*). Verified mintable live on
# sprint 2026-06-19 (see docs/gen3-token-research.md in the app repo).
_CLASSIC_TO_PLATFORM: dict[str, Optional[str]] = {
    "entities.read":        "storage:entities:read",
    "settings.read":        "settings:objects:read",
    "settings.write":       "settings:objects:write",
    "ReadConfig":           "settings:objects:read",
    "WriteConfig":          "settings:objects:write",
    "metrics.read":         "storage:metrics:read",
    "metrics.ingest":       "storage:metrics:write",
    "logs.read":            "storage:logs:read",
    "logs.ingest":          "storage:logs:write",
    "events.ingest":        "storage:events:write",
    # No platform-token equivalent → dropped (covered elsewhere / not needed for apponly):
    "activeGateTokenManagement.write":  None,   # → triggers ActiveGate-token pre-mint
    "activeGateTokenManagement.create": None,   # → triggers ActiveGate-token pre-mint
    "DataExport":           None,
    "InstallerDownload":    None,
    "openTelemetryTrace.ingest": None,          # platform OTel-ingest scope TBD (storage:spans:write invalid)
}
# Classic scopes meaning "this token must manage ActiveGate tokens" → on gen3 we pre-mint an
# ActiveGate token instead (the platform operator token can't carry it).
_AG_TRIGGER_SCOPES = {"activeGateTokenManagement.write", "activeGateTokenManagement.create"}


def to_platform_scopes(scopes: list[str]) -> tuple[list[str], bool]:
    """Translate classic apiToken scopes → platform-token scopes for gen3 minting.

    Returns (platform_scopes, needs_activegate_token). Scopes already in platform form
    (contain ':') pass through unchanged. Unknown classic scopes are dropped (logged)."""
    out: list[str] = []
    needs_ag = False
    for s in scopes:
        if s in _AG_TRIGGER_SCOPES:
            needs_ag = True
        if ":" in s:                      # already a platform scope
            out.append(s)
            continue
        if s in _CLASSIC_TO_PLATFORM:
            mapped = _CLASSIC_TO_PLATFORM[s]
            if mapped:
                out.append(mapped)
        else:
            log.warning("No gen3 platform-scope mapping for classic scope %r — dropped", s)
    return sorted(set(out)), needs_ag


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
