"""sync protect-main — Enable branch protection on main across repos.

Sets up branch protection rules:
  - Required status checks (integration tests must pass)
  - Strict mode (branch must be up to date before merging)
  - Enforce for admins
  - No deletions allowed
"""

import json
import subprocess
import sys

from sync.core.repos import load_repos, filter_sync_targets


PROTECTION_RULES = {
    "required_status_checks": {
        "strict": True,
        "contexts": [
            "codespaces-integration-test-with-dynatrace-deployment"
        ],
    },
    "enforce_admins": True,
    "allow_deletions": False,
    "required_pull_request_reviews": None,
    "restrictions": None,
}


def _protect(owner: str, name: str) -> tuple[bool, str]:
    """Apply branch protection to main. Returns (success, message)."""
    result = subprocess.run(
        ["gh", "api", "--method", "PUT",
         f"repos/{owner}/{name}/branches/main/protection",
         "--input", "-"],
        input=json.dumps(PROTECTION_RULES),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, "protected"


def _get_protection(owner: str, name: str) -> dict | None:
    """Get current branch protection. Returns None if unprotected."""
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{name}/branches/main/protection"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def run(args):
    target_repo = getattr(args, "repo", None)
    dry_run = args.dry_run

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"❌ '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = [r for r in repos if r.status == "active"]

    print(f"{'[DRY RUN] ' if dry_run else ''}Protecting main branch across {len(repos)} repos\n")

    for entry in repos:
        owner, name = entry.owner, entry.repo_name
        print(f"── {entry.repo} ──")

        # Check current status
        protection = _get_protection(owner, name)
        if protection:
            checks = protection.get("required_status_checks", {})
            contexts = checks.get("contexts", []) if checks else []
            enforce = protection.get("enforce_admins", {}).get("enabled", False)
            print(f"  📋 current: checks={contexts}, enforce_admins={enforce}")
        else:
            print(f"  📋 current: unprotected")

        if dry_run:
            print(f"  ⏳ would apply protection rules")
            print()
            continue

        ok, msg = _protect(owner, name)
        if ok:
            print(f"  🛡️  {msg}")
        else:
            print(f"  ❌ {msg}")
        print()
