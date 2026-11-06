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


# ── .devcontainer/delivery.yaml (ENH-163 F2/F3/F15) ─────────────────────────
# ONE file in the training repo that defines how it is delivered: the workshop
# type, whether learners get a personal environment (and which), and the shape
# of the shared machine. It outranks the index.md front-matter block; both
# present and disagreeing is refused (two truths), agreeing is a warning.
# Shared infrastructure itself stays derived from workshop-post-create.sh (D5).
#
#   version: 1
#   type: same-tenant            # same-tenant | multi-tenant  (tenancy single | multi)
#   availability: workshop-only  # workshop-only | self-service (default self-service)
#   seats:
#     personal: false            # learners get a personal environment (default: true;
#                                # false for a shared training, which cannot be true yet)
#     mode: full                 # full | dtctl-only  (default full)
#   shared:
#     instance: m6a.4xlarge      # only with workshop-post-create.sh; Orbital checks
#                                # it against its allowlist and ceiling at booking

DELIVERY_FILE = ".devcontainer/delivery.yaml"
TYPES = {"same-tenant": "single", "multi-tenant": "multi"}
SEAT_MODES = ("full", "dtctl-only")
_FILE_KEYS = ("version", "type", "availability", "seats", "shared")
_SEAT_KEYS = ("personal", "mode")
_SHARED_KEYS = ("instance",)
_INSTANCE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$")


def check_delivery_file(data, has_shared_create: bool) -> tuple[dict, list[str]]:
    """Check a parsed delivery.yaml. Returns ``(effective, issues)``; ``effective``
    carries availability/tenancy (the ENH-152 fields) plus type, seats and
    sharedInstance."""
    issues: list[str] = []
    # A shared training's learners have no personal environment until ENH-163 F7
    # (Orbital locks personalSeats to "0"), so that is its default.
    eff = {**DEFAULTS, "type": "", "seats": {"personal": not has_shared_create,
                                             "mode": "full"},
           "sharedInstance": ""}
    if not isinstance(data, dict):
        return eff, [f"{DELIVERY_FILE} must be a mapping"]
    for key in sorted(set(data) - set(_FILE_KEYS), key=str):
        issues.append(f"{DELIVERY_FILE}: unknown key {key!r} (allowed: {', '.join(_FILE_KEYS)})")
    if data.get("version") != 1:
        issues.append(f"{DELIVERY_FILE}: version must be 1 (got {data.get('version')!r})")
    t = data.get("type")
    if t is not None:
        if t in TYPES:
            eff["type"], eff["tenancy"] = t, TYPES[t]
        else:
            issues.append(f"{DELIVERY_FILE}: type {t!r} is not one of {', '.join(TYPES)}")
    a = data.get("availability")
    if a is not None:
        if a in AVAILABILITY:
            eff["availability"] = a
        else:
            issues.append(f"{DELIVERY_FILE}: availability {a!r} is not one of {', '.join(AVAILABILITY)}")
    seats = data.get("seats")
    if seats is not None:
        if not isinstance(seats, dict):
            issues.append(f"{DELIVERY_FILE}: seats must be a mapping")
        else:
            for key in sorted(set(seats) - set(_SEAT_KEYS), key=str):
                issues.append(f"{DELIVERY_FILE}: seats.{key}: unknown key")
            if "personal" in seats:
                if isinstance(seats["personal"], bool):
                    eff["seats"]["personal"] = seats["personal"]
                else:
                    issues.append(f"{DELIVERY_FILE}: seats.personal must be true or false")
            if "mode" in seats:
                if seats["mode"] in SEAT_MODES:
                    eff["seats"]["mode"] = seats["mode"]
                else:
                    issues.append(f"{DELIVERY_FILE}: seats.mode {seats['mode']!r} is not one of {', '.join(SEAT_MODES)}")
    shared = data.get("shared")
    if shared is not None:
        if not isinstance(shared, dict):
            issues.append(f"{DELIVERY_FILE}: shared must be a mapping")
        else:
            for key in sorted(set(shared) - set(_SHARED_KEYS), key=str):
                issues.append(f"{DELIVERY_FILE}: shared.{key}: unknown key")
            inst = shared.get("instance")
            if inst is not None:
                if isinstance(inst, str) and _INSTANCE.match(inst):
                    eff["sharedInstance"] = inst
                else:
                    issues.append(f"{DELIVERY_FILE}: shared.instance {inst!r} is not an instance type")
        if not has_shared_create:
            issues.append(f"{DELIVERY_FILE}: shared: needs {SHARED_CREATE} — the "
                          "script is what makes a training shared")
    if has_shared_create:
        if eff["availability"] != "workshop-only":
            issues.append(f"shared infrastructure ({SHARED_CREATE}) requires "
                          "availability: workshop-only")
        if eff["tenancy"] != "single":
            issues.append(f"shared infrastructure ({SHARED_CREATE}) requires "
                          "type: same-tenant — multi-tenant shared infrastructure is "
                          "not supported yet")
        if eff["seats"]["personal"]:
            issues.append(f"shared infrastructure ({SHARED_CREATE}) gives learners no "
                          "personal environment yet: seats.personal: true is not "
                          "available (ENH-163 F7)")
    return eff, issues


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

    ``.devcontainer/delivery.yaml`` (ENH-163) wins when present; an index.md
    block beside it must agree (a disagreement is an issue, agreement a
    warning printed by the command). Without the file, the index.md block as
    before. A missing index.md counts as "no block" (defaults), not as "nothing to
    check": a repo with a shared-infrastructure script and no index.md is still
    refused. ``effective`` is None only when nothing can be parsed.
    """
    path = Path(repo_path)
    index = index_md_path(path)
    has_create = (path / SHARED_CREATE).exists()
    has_start = (path / SHARED_START).exists()
    fm: dict = {}
    fm_error = ""
    if index.exists():
        try:
            fm = parse_front_matter(index.read_text())
        except DeliveryError as e:
            fm_error = f"{index.relative_to(path)}: {e}"
    dfile = path / DELIVERY_FILE
    if not dfile.exists():
        if fm_error:
            return None, [fm_error]
        return check_delivery(fm, has_shared_create=has_create,
                              has_shared_start=has_start)
    try:
        data = yaml.safe_load(dfile.read_text())
    except yaml.YAMLError as e:
        return None, [f"{DELIVERY_FILE}: not valid YAML ({str(e).splitlines()[0]})"]
    eff, issues = check_delivery_file(data, has_create)
    if has_start and not has_create:
        issues.append(f"{SHARED_START} without {SHARED_CREATE} — shared infrastructure is "
                      "defined by the create script; add it or remove the start script")
    block = fm.get("delivery") if isinstance(fm, dict) else None
    eff["warnings"] = []
    if isinstance(block, dict) and block:
        differ = [k for k in DEFAULTS if k in block and block[k] != eff[k]]
        if differ:
            issues.append(f"{DELIVERY_FILE} and the index.md delivery block disagree on "
                          f"{', '.join(differ)} — keep one (the file)")
        else:
            eff["warnings"].append("the index.md delivery block repeats delivery.yaml; "
                                   "remove it")
    eff["sharedInfra"] = has_create
    return eff, issues
