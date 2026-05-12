"""repos.yaml parsing and validation."""

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VALID_STATUSES = {"active", "archived", "experimental"}
VALID_IMAGE_TIERS = {"minimal", "k8s", "ai"}
REQUIRED_FIELDS = {"name", "repo", "status", "maintainer", "description"}


@dataclass
class RepoEntry:
    name: str
    repo: str
    status: str
    maintainer: str
    description: str
    architectures: list[str] = field(default_factory=lambda: ["amd64", "arm64"])
    sync_managed: bool = True
    ci: bool = True
    prebuilds: bool = False
    image_tier: str = "k8s"
    tags: list[str] = field(default_factory=list)
    # Registry fields (used by the org GitHub Pages site)
    title: str = ""
    primary_tag: str = ""
    icon_key: str = ""
    duration: str = ""
    is_template: bool = False
    listed: bool = True

    @property
    def owner(self) -> str:
        return self.repo.split("/")[0]

    @property
    def repo_name(self) -> str:
        return self.repo.split("/")[1]

    @property
    def url(self) -> str:
        return f"github.com/{self.repo}"


def load_repos(path: Optional[Path] = None) -> list[RepoEntry]:
    """Load and parse repos.yaml from the given path or default location."""
    import dataclasses
    if path is None:
        path = Path(__file__).parent.parent.parent / "repos.yaml"
    with open(path) as f:
        data = yaml.safe_load(f)
    known = {f.name for f in dataclasses.fields(RepoEntry)}
    return [RepoEntry(**{k: v for k, v in r.items() if k in known}) for r in data.get("repos", [])]


def validate_repos(repos: list[RepoEntry]) -> list[str]:
    """Validate repos entries. Returns list of error messages."""
    errors = []
    seen_names = set()

    for r in repos:
        if r.name in seen_names:
            errors.append(f"{r.name}: duplicate name")
        seen_names.add(r.name)

        if r.status not in VALID_STATUSES:
            errors.append(
                f"{r.name}: status '{r.status}' invalid; use {'/'.join(sorted(VALID_STATUSES))}"
            )

        if r.image_tier not in VALID_IMAGE_TIERS:
            errors.append(
                f"{r.name}: image_tier '{r.image_tier}' invalid; use {'/'.join(sorted(VALID_IMAGE_TIERS))}"
            )

        if "/" not in r.repo:
            errors.append(f"{r.name}: repo '{r.repo}' must be in owner/name format")

        if not r.maintainer.startswith("@"):
            errors.append(
                f"{r.name}: maintainer '{r.maintainer}' should start with @"
            )

    return errors


def filter_sync_targets(repos: list[RepoEntry]) -> list[RepoEntry]:
    """Return repos eligible for sync push-update."""
    return [r for r in repos if r.sync_managed and r.status == "active"]
