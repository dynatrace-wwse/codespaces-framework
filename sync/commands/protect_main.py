"""sync protect-main — Enable branch protection on the default branch across repos.

Sets up branch protection rules:
  - Required status checks (integration tests must pass)
  - Strict mode (branch must be up to date before merging)
  - Enforce for admins
  - No deletions allowed
"""

import dataclasses
import json
import subprocess
import sys

from sync.core.repos import RepoEntry, load_repos


DEFAULT_CONTEXTS = ["codespaces-integration-test-with-dynatrace-deployment"]


def _get_default_branch(owner: str, name: str) -> str | None:
    """Return the repo's default branch, or None on failure."""
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{name}", "-q", ".default_branch"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _build_rules(contexts: list[str]) -> dict:
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": contexts,
        },
        "enforce_admins": True,
        "allow_deletions": False,
        "required_pull_request_reviews": None,
        "restrictions": None,
    }


def _protect(owner: str, name: str, branch: str, contexts: list[str]) -> tuple[bool, str]:
    """Apply branch protection. Returns (success, message)."""
    result = subprocess.run(
        ["gh", "api", "--method", "PUT",
         f"repos/{owner}/{name}/branches/{branch}/protection",
         "--input", "-"],
        input=json.dumps(_build_rules(contexts)),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, "protected"


def _get_protection(owner: str, name: str, branch: str) -> dict | None:
    """Get current branch protection. Returns None if unprotected."""
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{name}/branches/{branch}/protection"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def run(args):
    target_repo = getattr(args, "repo", None)
    branch_override = getattr(args, "branch", None)
    custom_contexts = getattr(args, "check", None) or []
    dry_run = args.dry_run

    contexts = list(custom_contexts) if custom_contexts else DEFAULT_CONTEXTS

    repos = load_repos()
    if target_repo:
        matched = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                   or r.repo == target_repo]
        if matched:
            repos = matched
        elif "/" in target_repo:
            # Explicit owner/name not in repos.yaml — create a synthetic entry
            owner_part, name_part = target_repo.split("/", 1)
            repos = [RepoEntry(
                name=name_part,
                repo=target_repo,
                status="active",
                maintainer="",
                description="",
            )]
        else:
            print(f"❌ '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = [r for r in repos if r.status == "active"]

    print(f"{'[DRY RUN] ' if dry_run else ''}Protecting default branch across {len(repos)} repos")
    print(f"  required checks: {contexts}\n")

    for entry in repos:
        owner, name = entry.owner, entry.repo_name
        print(f"── {entry.url} ──")

        branch = branch_override or _get_default_branch(owner, name) or "main"
        print(f"  🌿 branch: {branch}")

        protection = _get_protection(owner, name, branch)
        if protection:
            checks = protection.get("required_status_checks", {})
            existing_contexts = checks.get("contexts", []) if checks else []
            enforce = protection.get("enforce_admins", {}).get("enabled", False)
            print(f"  📋 current: checks={existing_contexts}, enforce_admins={enforce}")
        else:
            print(f"  📋 current: unprotected")

        if dry_run:
            print(f"  ⏳ would apply protection rules")
            print()
            continue

        ok, msg = _protect(owner, name, branch, contexts)
        if ok:
            print(f"  🛡️  {msg}")
        else:
            print(f"  ❌ {msg}")
        print()
