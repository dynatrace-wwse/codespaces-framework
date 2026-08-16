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
from dataclasses import dataclass, field
from typing import Optional

import httpx
import yaml

from shared.log_safety import scrub_for_log

log = logging.getLogger("ops-provisioning")

_GH_RAW = "https://raw.githubusercontent.com/{repo}/{ref}/.devcontainer/yaml/dt-tokens.yaml"
_FW_RAW = "https://raw.githubusercontent.com/dynatrace-wwse/codespaces-framework/main/.devcontainer/yaml/dt-tokens.yaml"


@dataclass
class TokenSpec:
    name_suffix: str    # e.g. "operator"
    env_var: str        # e.g. "DT_OPERATOR_TOKEN"
    scopes: list[str]
    # Token FAMILY. "classic" mints a dt0c01 Api-Token (translated to a platform token
    # on tenants that retired classic creation); "platform" always mints a dt0s16 via
    # the Account Management API, because the consumer calls an endpoint that accepts
    # nothing else. A repo may declare both — the CI/CD workshop needs monaco/dtctl
    # (platform) alongside the SDLC event helpers (classic) in the same session.
    kind: str = "classic"
    # Further env vars that receive the SAME token value. Mirrors aliasEnvVars in the
    # app's TokenSpec; lets one minted token serve several consumer variables.
    aliases: list[str] = field(default_factory=list)


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
# platform-scope equivalent (a single scope, or a LIST when one classic capability spans
# several gen3-native scopes). A value of None = no platform equivalent (covered elsewhere /
# not needed). Verified mintable live on sprint (see docs/gen3-token-research.md).
#
# InstallerDownload → the operator's "fetch agents + connection info" capability. On operator
# >= 1.10 the DynaKube controller calls the fleet-management connection-info + container-image
# endpoints and 403s without these scopes ("OAuth token is missing required scope"), so the
# ActiveGate StatefulSet is never created and OneAgent injection fails. `environment-api:
# deployment:download` (the classic alternative the operator names) is NOT a valid platform-
# token scope (account API 400 "Invalid scopes") — the fleet-management scopes are the gen3
# equivalents (all four mint 200 on sprint 2026-08-02). Root-caused via a live 1.10.2 session.
_CLASSIC_TO_PLATFORM: dict[str, Optional[object]] = {
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
    "InstallerDownload": [
        "fleet-management:oneagents:download",
        "fleet-management:oneagent.connection-info:read",
        "fleet-management:activegate.connection-info:read",
        "fleet-management:container-images:read",
    ],
    # Operator >= 1.10 mints its OWN ActiveGate auth token via POST /api/v2/activeGateTokens
    # and 403s without these on its token (the pre-minted AG token is not used on 1.10.x).
    # Also still triggers the AG pre-mint below (harmless redundancy). Grantable on sprint.
    "activeGateTokenManagement.write":  "fleet-management:activegate.tokens:write",
    "activeGateTokenManagement.create": "fleet-management:activegate.tokens:create",
    # No platform-token equivalent → dropped (covered elsewhere / not needed for apponly):
    "DataExport":           None,
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
            if isinstance(mapped, list):
                out.extend(mapped)
            elif mapped:
                out.append(mapped)
        else:
            log.warning("No gen3 platform-scope mapping for classic scope %r — dropped", s)
    return sorted(set(out)), needs_ag


def _parse_yaml(content: str) -> list[TokenSpec]:
    data = yaml.safe_load(content)
    specs = []
    for t in data.get("tokens", []):
        # An unrecognised kind is a typo in a file we do not control the review of.
        # Fall back to classic rather than skipping the token: a missing token fails
        # the session, a classic one fails loudly at the first call with a real error.
        kind = str(t.get("kind", "classic")).strip().lower() or "classic"
        if kind not in ("classic", "platform"):
            log.warning("Unknown token kind %r for %r — treating as classic",
                        kind, t.get("env_var"))
            kind = "classic"
        specs.append(TokenSpec(
            name_suffix=t["name_suffix"],
            env_var=t["env_var"],
            scopes=t.get("scopes", []),
            kind=kind,
            aliases=list(t.get("aliases", []) or []),
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
                    log.info("Loaded token specs from %s (%d tokens)", scrub_for_log(url), len(specs))
                    return specs
            except Exception as exc:
                log.debug("Could not fetch %s: %s", scrub_for_log(url), scrub_for_log(exc))

    log.info("Using hardcoded DEFAULT_SPECS (no remote spec found)")
    return DEFAULT_SPECS
