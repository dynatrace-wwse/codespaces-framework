"""Training delivery block — the declared half of a training's categories (ENH-152).

A training declares how it may be delivered in the YAML front matter of its
``<docs_dir>/index.md`` (default ``docs/index.md``), next to the ENH-062 catalog
fields the editor already writes::

    ---
    description: "…"
    tags: [interactive]
    delivery:
      availability: workshop-only   # self-service (default) | workshop-only
      tenancy: single               # any (default) | single | multi
    ---

Shared infrastructure is NOT declared: it is derived from the presence of
``.devcontainer/workshop-post-create.sh`` (owner decision D5). The rule for v1
(D6, D10) is that a training with shared infrastructure must be
``workshop-only`` AND ``single``-tenant — multi-tenant shared is not built yet.

This module only checks. Nothing reads the block at runtime yet (step 1 of
ENH-152); Orbital and the app start reading it together in step 2. An ABSENT
block is valid and means today's behaviour (D4), so no existing training has to
change. A PRESENT but wrong block is refused: a typo such as ``availabilty``
must not silently fall back to the default, because the default is the most
permissive answer (self-service, any tenancy).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

AVAILABILITY = ("self-service", "workshop-only")
TENANCY = ("any", "single", "multi")
DEFAULTS = {"availability": "self-service", "tenancy": "any"}

SHARED_CREATE = ".devcontainer/workshop-post-create.sh"
SHARED_START = ".devcontainer/workshop-post-start.sh"

_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_DOCS_DIR = re.compile(r"^docs_dir:\s*['\"]?([^'\"#\s]+)", re.M)


class DeliveryError(ValueError):
    """The front matter could not be read as YAML at all."""


def parse_front_matter(text: str) -> dict:
    """Return the front-matter mapping of a markdown file ({} when it has none).

    Raises DeliveryError when a front matter block exists but is not a YAML
    mapping — the importer would drop it and every field in it would vanish.
    """
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise DeliveryError(f"front matter is not valid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DeliveryError("front matter is not a YAML mapping")
    return data


def check_delivery(front_matter: dict, has_shared_create: bool,
                   has_shared_start: bool = False) -> tuple[dict, list[str]]:
    """Check a training's declared delivery against the v1 rules.

    Returns ``(effective, issues)``: ``effective`` is the resolved
    ``{availability, tenancy, sharedInfra}`` (defaults filled in), ``issues``
    a list of human-readable problems — empty means valid.
    """
    issues: list[str] = []
    effective = dict(DEFAULTS)
    block = front_matter.get("delivery") if front_matter else None

    if block is not None:
        if not isinstance(block, dict):
            issues.append("delivery must be a mapping with availability/tenancy keys")
            block = {}
        for key in sorted(set(block) - set(DEFAULTS), key=str):
            issues.append(
                f"delivery.{key}: unknown key (allowed: {', '.join(DEFAULTS)}) — "
                "a misspelt key would silently fall back to the default"
            )
        for key, allowed in (("availability", AVAILABILITY), ("tenancy", TENANCY)):
            if key not in block:
                continue
            value = block[key]
            if not isinstance(value, str) or value not in allowed:
                issues.append(
                    f"delivery.{key}: {value!r} is not one of {', '.join(allowed)}"
                )
            else:
                effective[key] = value

    shared = bool(has_shared_create)
    effective["sharedInfra"] = shared

    if has_shared_start and not has_shared_create:
        issues.append(
            f"{SHARED_START} without {SHARED_CREATE} — shared infrastructure is "
            "defined by the create script; add it or remove the start script"
        )

    if shared:
        if effective["availability"] != "workshop-only":
            issues.append(
                f"shared infrastructure ({SHARED_CREATE}) requires "
                "delivery.availability: workshop-only — a shared machine needs an "
                "owning workshop"
            )
        if effective["tenancy"] != "single":
            issues.append(
                f"shared infrastructure ({SHARED_CREATE}) requires "
                "delivery.tenancy: single — multi-tenant shared infrastructure is "
                f"not supported yet (got {effective['tenancy']!r})"
            )

    return effective, issues


def index_md_path(repo_path) -> Path:
    """``<docs_dir>/index.md`` for a training repo (mkdocs ``docs_dir``, else docs)."""
    path = Path(repo_path)
    docs_dir = "docs"
    for name in ("mkdocs.yaml", "mkdocs.yml"):
        mk = path / name
        if mk.exists():
            m = _DOCS_DIR.search(mk.read_text())
            if m:
                docs_dir = m.group(1).strip("/")
            break
    return path / docs_dir / "index.md"


def check_repo(repo_path) -> tuple[Optional[dict], list[str]]:
    """Check a local training repo. Returns ``(effective, issues)``.

    A missing index.md counts as "no block" (defaults), not as "nothing to
    check": a repo with a shared-infrastructure script and no index.md is still
    refused. ``effective`` is None only when the front matter cannot be parsed.
    """
    path = Path(repo_path)
    index = index_md_path(path)
    fm: dict = {}
    if index.exists():
        try:
            fm = parse_front_matter(index.read_text())
        except DeliveryError as e:
            return None, [f"{index.relative_to(path)}: {e}"]
    return check_delivery(
        fm,
        has_shared_create=(path / SHARED_CREATE).exists(),
        has_shared_start=(path / SHARED_START).exists(),
    )
