"""Content distribution service (Phase 1 of multi-tenant content delivery).

Orbital is the ONLY place that holds the GitHub token. Tenants pull curated
*profiles* and proxied repo content from here using a per-install *content key*
(header ``X-Content-Key``) — never a GitHub token, never a per-user secret.

Security model (important): the raw proxy injects the Orbital GitHub token, so it
would otherwise be an open read of every private dynatrace-wwse repo. Two guards:
  1. A valid content key is REQUIRED on every endpoint.
  2. The raw proxy only serves ``owner/repo`` pairs referenced by a profile
     (allowlist), and rejects path traversal.

Routes (mounted under /api/content, reachable server-to-server via the nginx
catch-all, same as /api/arena/*):
  GET /api/content/profiles/{profile_id}
  GET /api/content/repos/{owner}/{repo}/raw/{path}
"""

import json
import logging
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("ops-dashboard.content")

GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

# Comma-separated allowed content keys. Tenants present one as X-Content-Key.
# These gate access to the proxy (which holds the GitHub token) — keep them set.
CONTENT_KEYS = {
    k.strip() for k in os.environ.get("CONTENT_KEYS", "").split(",") if k.strip()
}

PROFILES_DIR = Path(__file__).parent.parent / "content" / "profiles"

router = APIRouter(prefix="/api/content", tags=["content"])


def _require_key(content_key: str | None) -> None:
    if not CONTENT_KEYS:
        # Fail closed: with no keys configured the proxy must not be reachable.
        raise HTTPException(503, "Content service not configured (no CONTENT_KEYS).")
    if not content_key or content_key not in CONTENT_KEYS:
        raise HTTPException(401, "Invalid or missing X-Content-Key.")


def _require_writer(x_auth_user: str | None) -> None:
    """Profile management is org-member-only. nginx sets X-Auth-User only after
    oauth2-proxy validates org membership (auth_request), so presence ⇒ writer."""
    if not x_auth_user:
        raise HTTPException(401, "Writer authentication required.")


def _valid_id(profile_id: str) -> bool:
    return bool(profile_id) and profile_id.replace("-", "").replace("_", "").isalnum()


def _load_profile(profile_id: str) -> dict:
    # Reject path traversal in the profile id.
    if not _valid_id(profile_id):
        raise HTTPException(400, "Invalid profile id.")
    path = PROFILES_DIR / f"{profile_id}.json"
    if not path.is_file():
        raise HTTPException(404, f"Profile '{profile_id}' not found.")
    return json.loads(path.read_text())


def _allowed_repos() -> set[str]:
    """owner/repo allowlist = every repo referenced by any profile."""
    repos: set[str] = set()
    if PROFILES_DIR.is_dir():
        for f in PROFILES_DIR.glob("*.json"):
            try:
                for src in json.loads(f.read_text()).get("sources", []):
                    if src.get("repo"):
                        repos.add(src["repo"].lower())
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Bad profile %s: %s", f.name, exc)
    return repos


async def _latest_sha(owner: str, repo: str, branch: str) -> str | None:
    url = f"{GH_API}/repos/{owner}/{repo}/commits/{branch}?per_page=1"
    headers = {"Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return r.json().get("sha")
    except Exception as exc:
        log.warning("sha fetch failed %s/%s@%s: %s", owner, repo, branch, exc)
    return None


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: str, x_content_key: str | None = Header(default=None)):
    """Return a profile manifest: its sources plus each repo's latest commit sha
    (so the tenant app can diff and refresh)."""
    _require_key(x_content_key)
    profile = _load_profile(profile_id)
    sources = []
    for src in profile.get("sources", []):
        repo_full = src.get("repo", "")
        branch = src.get("branch", "main")
        owner, _, repo = repo_full.partition("/")
        sha = await _latest_sha(owner, repo, branch) if owner and repo else None
        sources.append({
            "key": src.get("key"),
            "category": src.get("category"),
            "categoryLabel": src.get("categoryLabel"),
            "repo": repo_full,
            "branch": branch,
            "version": sha,
        })
    return {"profileId": profile.get("profileId", profile_id), "sources": sources}


@router.get("/repos/{owner}/{repo}/raw/{path:path}")
async def proxy_raw(
    owner: str,
    repo: str,
    path: str,
    ref: str = "main",
    x_content_key: str | None = Header(default=None),
):
    """Proxy a raw file from a profile-allowlisted private repo, injecting the
    Orbital GitHub token. The tenant never sees the token."""
    _require_key(x_content_key)

    repo_full = f"{owner}/{repo}".lower()
    if repo_full not in _allowed_repos():
        raise HTTPException(403, "Repository not in any profile.")
    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "Invalid path.")
    if ".." in ref or "/" in ref:
        raise HTTPException(400, "Invalid ref.")

    url = f"{RAW_BASE}/{owner}/{repo}/{ref}/{path}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}"} if GH_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=headers)
    except Exception as exc:
        raise HTTPException(502, f"Upstream fetch failed: {exc}")
    if r.status_code == 404:
        raise HTTPException(404, "File not found.")
    if r.status_code >= 400:
        raise HTTPException(502, f"Upstream {r.status_code}.")
    # Pass content through as-is (markdown / yaml / etc).
    from fastapi.responses import Response
    return Response(content=r.content, media_type=r.headers.get("content-type", "text/plain"))


# ── Profile management (writer-gated; org members only, via nginx X-Auth-User) ──

@router.get("/admin/profiles")
async def list_profiles(x_auth_user: str | None = Header(default=None)):
    """List all profiles (id, description, sources) for the management UI."""
    _require_writer(x_auth_user)
    profiles = []
    if PROFILES_DIR.is_dir():
        for f in sorted(PROFILES_DIR.glob("*.json")):
            try:
                profiles.append(json.loads(f.read_text()))
            except Exception as exc:
                log.warning("Bad profile %s: %s", f.name, exc)
    return {"profiles": profiles}


@router.put("/admin/profiles/{profile_id}")
async def put_profile(profile_id: str, body: dict, x_auth_user: str | None = Header(default=None)):
    """Create or update a profile JSON file. Validates each source has a valid repo."""
    _require_writer(x_auth_user)
    if not _valid_id(profile_id):
        raise HTTPException(400, "Invalid profile id (use [a-z0-9-_]).")
    sources = body.get("sources")
    if not isinstance(sources, list) or not sources:
        raise HTTPException(400, "Profile must have a non-empty 'sources' array.")
    clean = []
    for s in sources:
        if not isinstance(s, dict) or "/" not in str(s.get("repo", "")):
            raise HTTPException(400, "Each source needs a 'repo' as owner/repo.")
        clean.append({
            "key": s.get("key") or s["repo"].split("/")[-1],
            "category": s.get("category") or "uncategorized",
            "categoryLabel": s.get("categoryLabel") or s.get("category") or "Content",
            "repo": s["repo"],
            "branch": s.get("branch") or "main",
        })
    profile = {"profileId": profile_id, "description": body.get("description", ""), "sources": clean}
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILES_DIR / f"{profile_id}.json").write_text(json.dumps(profile, indent=2) + "\n")
    log.info("Profile '%s' saved by %s (%d sources)", profile_id, x_auth_user, len(clean))
    return {"ok": True, "profileId": profile_id, "sources": len(clean)}


@router.delete("/admin/profiles/{profile_id}")
async def delete_profile(profile_id: str, x_auth_user: str | None = Header(default=None)):
    _require_writer(x_auth_user)
    if not _valid_id(profile_id) or profile_id in ("all", "default"):
        raise HTTPException(400, "Cannot delete this profile.")
    path = PROFILES_DIR / f"{profile_id}.json"
    if path.is_file():
        path.unlink()
    return {"ok": True}
