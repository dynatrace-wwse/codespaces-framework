"""Content distribution service (multi-tenant content delivery).

Orbital is the ONLY place that holds the GitHub token. Tenants pull curated
*profiles* and proxied repo content from here. Authentication is by **Dynatrace
domain**: content can only be pulled by a Dynatrace tenant (production, sprint, or
development apps domain). The caller passes its tenant URL; the service validates the
domain suffix, derives the tenant id, and resolves which profile to deliver from a
tenant→profile table (with per-domain defaults). No per-tenant key.

Security model (important): the raw proxy injects the Orbital GitHub token, so it
would otherwise be an open read of every private dynatrace-wwse repo. Guards:
  1. The caller must present a Dynatrace tenant URL (prod/sprint/dev domain suffix).
  2. The raw proxy only serves ``owner/repo`` pairs referenced by a profile
     (allowlist), and rejects path traversal.
  Note: the tenant URL is a claim (server-to-server call from AppEngine). The content
  is internal enablement material; for stronger isolation, restrict by AppEngine egress
  IPs at the edge. The domain suffix check stops arbitrary internet callers.

Routes (mounted under /api/content, reachable server-to-server via the nginx
catch-all, same as /api/arena/*):
  GET /api/content/manifest?tenant=<tenantUrl>        — resolves the tenant's profile
  GET /api/content/repos/{owner}/{repo}/raw/{path}?tenant=<tenantUrl>
  GET/PUT /api/content/admin/profiles, /api/content/admin/tenant-map  (writer-gated)
"""

import asyncio
import io
import json
import logging
import os
import re
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

log = logging.getLogger("ops-dashboard.content")

GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

# Allowed Dynatrace apps-domain suffixes → environment class. Env-overridable
# (CONTENT_DOMAIN_SUFFIXES="suffix:class,suffix:class").
_DEFAULT_DOMAINS = {
    ".apps.dynatrace.com": "prod",
    ".sprint.apps.dynatracelabs.com": "sprint",
    ".dev.apps.dynatracelabs.com": "dev",
}
def _load_domain_suffixes() -> dict:
    raw = os.environ.get("CONTENT_DOMAIN_SUFFIXES", "")
    if not raw:
        return dict(_DEFAULT_DOMAINS)
    out = {}
    for pair in raw.split(","):
        suffix, _, cls = pair.strip().partition(":")
        if suffix and cls:
            out[suffix] = cls
    return out or dict(_DEFAULT_DOMAINS)
DOMAIN_SUFFIXES = _load_domain_suffixes()

# Where the *mutable* content state lives. The admin endpoints below write
# profiles, the tenant map and the source catalog at runtime, so this directory
# must NOT sit inside the git checkout: deployment is `git pull --ff-only`, and
# a tracked file the running service has rewritten aborts the pull. Point
# CONTENT_STATE_DIR at a directory outside the repo (the ops boxes use
# /home/ops/content); the in-repo copy then serves only as seed defaults.
_CONTENT_DIR = Path(os.environ.get("CONTENT_STATE_DIR")
                    or Path(__file__).parent.parent / "content")

PROFILES_DIR = _CONTENT_DIR / "profiles"
TENANT_MAP_FILE = _CONTENT_DIR / "tenant_map.json"
# Managed training-source catalog: repos Orbital delivers (incl. private ones added for
# customer workshops). Distinct from profiles — this is the master list you add/validate/
# remove in the Trainings tab; profiles reference these repos.
SOURCES_FILE = _CONTENT_DIR / "sources.json"

router = APIRouter(prefix="/api/content", tags=["content"])


def classify_tenant(tenant_url: str | None) -> tuple[str, str]:
    """Validate a Dynatrace tenant URL and return (tenant_id, domain_class).
    Raises 403 if the domain is not a recognised Dynatrace apps domain."""
    if not tenant_url:
        raise HTTPException(403, "Missing tenant URL.")
    host = urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}").hostname or ""
    host = host.lower()
    for suffix, cls in DOMAIN_SUFFIXES.items():
        if host.endswith(suffix):
            tenant_id = host[: -len(suffix)].split(".")[0]
            # Sprint/dev env URLs carry a cluster suffix, e.g. 'ydi9582h-1' — strip it
            # so the id matches the tenant_map key ('ydi9582h'). Otherwise the lookup
            # misses and the tenant falls back to the domain default profile.
            tenant_id = re.sub(r"-\d+$", "", tenant_id)
            if tenant_id:
                return tenant_id, cls
    raise HTTPException(403, "Content is only served to Dynatrace tenants (prod/sprint/dev).")


def _load_tenant_map() -> dict:
    if TENANT_MAP_FILE.is_file():
        try:
            return json.loads(TENANT_MAP_FILE.read_text())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Bad tenant_map.json: %s", exc)
    return {"defaults": {"prod": "all", "sprint": "all", "dev": "all"}, "tenants": {}}


def resolve_profile(tenant_id: str, domain_class: str) -> str:
    """tenant-specific mapping wins; else the per-domain default; else 'all'."""
    m = _load_tenant_map()
    return (m.get("tenants", {}).get(tenant_id)
            or m.get("defaults", {}).get(domain_class)
            or "all")


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


def _read_profiles() -> list[dict]:
    """Every profile on disk, each stamped with the id the rest of the service uses.

    The FILENAME is the profile id: `_load_profile`, `put_profile`, `delete_profile` and
    `put_tenant_map`'s validation all key off `{id}.json`. A `profileId` inside the file
    that disagrees (hand-edited on the box, or a rename that rewrote the body but not the
    name) used to be echoed straight back to the console, which then showed an id that no
    write path could resolve: `delete` looked for a file that did not exist, found nothing,
    and reported success forever, while the delivery table rejected the same id as unknown.
    Stamping the stem here is what keeps read and write talking about the same profile.
    """
    profiles: list[dict] = []
    if not PROFILES_DIR.is_dir():
        return profiles
    for f in sorted(PROFILES_DIR.glob("*.json")):
        try:
            p = json.loads(f.read_text())
        except Exception as exc:
            log.warning("Bad profile %s: %s", f.name, exc)
            continue
        if p.get("profileId") != f.stem:
            log.warning("Profile %s declares profileId '%s'; using the filename.",
                        f.name, p.get("profileId"))
        p["profileId"] = f.stem
        profiles.append(p)
    return profiles


def _allowed_repos() -> set[str]:
    """owner/repo allowlist = every repo referenced by any profile + every managed source."""
    repos: set[str] = set()
    if PROFILES_DIR.is_dir():
        for f in PROFILES_DIR.glob("*.json"):
            try:
                for src in json.loads(f.read_text()).get("sources", []):
                    if src.get("repo"):
                        repos.add(src["repo"].lower())
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Bad profile %s: %s", f.name, exc)
    for s in _load_sources():
        if s.get("repo"):
            repos.add(s["repo"].lower())
    return repos


# ── Managed training-source catalog ─────────────────────────────────────────────

def _parse_repo_ref(url_or_full: str) -> tuple[str | None, str | None]:
    """Normalize a GitHub repo reference to ("owner/repo", branch|None). Accepts a
    full URL (https://github.com/owner/repo[.git][/tree/branch[/...]]) or a bare
    "owner/repo". A /tree/{branch} suffix yields the branch so trainings can be
    loaded straight from a branch URL (test-before-merge)."""
    s = (url_or_full or "").strip()
    if not s:
        return None, None
    s = re.sub(r"^https?://(www\.)?github\.com/", "", s)
    s = re.sub(r"\.git$", "", s).strip("/")
    parts = s.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None, None
    owner, repo = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", owner) or not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        return None, None
    branch = None
    if len(parts) > 3 and parts[2] == "tree":
        branch = "/".join(parts[3:]) or None
    return f"{owner}/{repo}", branch


def _parse_repo(url_or_full: str) -> str | None:
    """Normalize a GitHub repo reference to "owner/repo" (see _parse_repo_ref)."""
    return _parse_repo_ref(url_or_full)[0]


def _load_sources() -> list[dict]:
    if SOURCES_FILE.is_file():
        try:
            return json.loads(SOURCES_FILE.read_text()).get("sources", [])
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Bad sources.json: %s", exc)
    return []


def _save_sources(sources: list[dict]) -> None:
    SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_FILE.write_text(json.dumps({"sources": sources}, indent=2) + "\n")


async def _gh_get(path: str):
    headers = {"Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    async with httpx.AsyncClient(timeout=12) as client:
        return await client.get(f"{GH_API}{path}", headers=headers)


async def _validate_repo(repo_full: str, branch: str = "main") -> dict:
    """Probe GitHub: repo reachable (with Orbital's token, so private repos count) and it
    looks like a training repo (mkdocs.yml/.yaml = self-paced, .devcontainer = hands-on).
    Returns {valid, reason, delivery, hasMkdocs, hasDevcontainer, defaultBranch}."""
    owner, _, repo = repo_full.partition("/")
    try:
        r = await _gh_get(f"/repos/{owner}/{repo}")
        if r.status_code == 404:
            return {"valid": False, "reason": "Repository not found (or Orbital's token can't see it)."}
        if r.status_code == 403:
            return {"valid": False, "reason": "Access denied — Orbital's GitHub token lacks access to this repo."}
        if r.status_code != 200:
            return {"valid": False, "reason": f"GitHub returned HTTP {r.status_code}."}
        default_branch = r.json().get("default_branch", "main")
        use_branch = branch or default_branch
        tree = await _gh_get(f"/repos/{owner}/{repo}/git/trees/{use_branch}?recursive=0")
        paths = [e.get("path", "") for e in (tree.json().get("tree", []) if tree.status_code == 200 else [])]
        has_mkdocs = any(p in ("mkdocs.yml", "mkdocs.yaml") for p in paths)
        has_devc = any(p == ".devcontainer" or p.startswith(".devcontainer") for p in paths)
        if not has_mkdocs and not has_devc:
            return {"valid": False, "reason": "Not a training repo — no mkdocs.yml or .devcontainer found.",
                    "defaultBranch": default_branch}
        return {"valid": True, "reason": "Validated.", "defaultBranch": default_branch,
                "hasMkdocs": has_mkdocs, "hasDevcontainer": has_devc,
                "delivery": "hands-on" if has_devc else "self-paced"}
    except Exception as exc:
        return {"valid": False, "reason": f"Validation error: {exc}"}


# Latest-commit-sha cache: (owner, repo, branch) → (sha, fetched_at). The manifest
# is called on every app boot (3×/boot today) by every tenant; without this each
# call was 1 serial GitHub round-trip per profile source (~21 × 350ms ≈ 8s).
# Content changes land on git push, so a short TTL only delays pickup by ≤60s.
_SHA_CACHE: dict[tuple[str, str, str], tuple[str | None, float]] = {}
_SHA_TTL = 60.0
_SHA_NEG_TTL = 15.0  # failed lookups retry sooner
_SHA_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}


def _sha_fresh(cached: tuple[str | None, float] | None) -> bool:
    if cached is None:
        return False
    sha, ts = cached
    return time.monotonic() - ts < (_SHA_TTL if sha is not None else _SHA_NEG_TTL)


async def _latest_sha(owner: str, repo: str, branch: str) -> str | None:
    key = (owner, repo, branch)
    cached = _SHA_CACHE.get(key)
    if _sha_fresh(cached):
        return cached[0]
    # Single-flight per repo@branch: when the TTL lapses under load, N concurrent
    # callers otherwise all hit GitHub at once (observed as a 7-10s manifest tail
    # in storm testing). One caller refreshes; the rest serve the stale sha —
    # content changes are picked up within one TTL either way.
    lock = _SHA_LOCKS.setdefault(key, asyncio.Lock())
    if lock.locked() and cached is not None:
        return cached[0]
    async with lock:
        cached = _SHA_CACHE.get(key)
        if _sha_fresh(cached):
            return cached[0]
        url = f"{GH_API}/repos/{owner}/{repo}/commits/{branch}?per_page=1"
        headers = {"Accept": "application/vnd.github+json"}
        if GH_TOKEN:
            headers["Authorization"] = f"Bearer {GH_TOKEN}"
        sha = None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    sha = r.json().get("sha")
        except Exception as exc:
            log.warning("sha fetch failed %s/%s@%s: %s", owner, repo, branch, exc)
            # Keep serving the previous sha (if any) rather than flapping to None.
            if cached is not None and cached[0] is not None:
                return cached[0]
        _SHA_CACHE[key] = (sha, time.monotonic())
        return sha


async def _build_sources(profile: dict) -> list[dict]:
    """Expand a profile's sources with each repo's latest commit sha.

    Branch resolution: the managed catalog (Trainings tab) is authoritative — a
    branch switch there takes effect for every profile on the next manifest,
    without re-saving profiles. Profiles keep their own branch as fallback for
    repos not in the catalog."""
    managed_branch = {
        s.get("repo", "").lower(): s.get("branch")
        for s in _load_sources() if s.get("repo") and s.get("branch")
    }
    entries = []
    for src in profile.get("sources", []):
        repo_full = src.get("repo", "")
        branch = managed_branch.get(repo_full.lower()) or src.get("branch", "main")
        owner, _, repo = repo_full.partition("/")
        entries.append((src, repo_full, branch, owner, repo))
    # Fetch all shas concurrently — serial fetching cost ~350ms × N sources
    # (≈8s for a 21-repo profile) on every manifest call.
    shas = await asyncio.gather(*[
        _latest_sha(owner, repo, branch) if owner and repo else asyncio.sleep(0)
        for (_, _, branch, owner, repo) in entries
    ])
    return [{
        "key": src.get("key"),
        "category": src.get("category"),
        "categoryLabel": src.get("categoryLabel"),
        "repo": repo_full,
        "branch": branch,
        "version": sha,
    } for (src, repo_full, branch, _, _), sha in zip(entries, shas)]


@router.get("/manifest")
async def get_manifest(tenant: str | None = None):
    """Resolve the calling tenant's profile (by tenant id + domain) and return its
    sources + commit shas. The app calls this with its own environment URL as `tenant`."""
    tenant_id, domain_class = classify_tenant(tenant)
    profile_id = resolve_profile(tenant_id, domain_class)
    profile = _load_profile(profile_id)
    log.info("Manifest: tenant=%s (%s) → profile=%s", tenant_id, domain_class, profile_id)
    return {"profileId": profile_id, "tenant": tenant_id, "domain": domain_class,
            "sources": await _build_sources(profile)}


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: str, tenant: str | None = None):
    """Return a specific profile's manifest (tenant-domain gated). Mostly for preview."""
    classify_tenant(tenant)
    return {"profileId": profile_id, "sources": await _build_sources(_load_profile(profile_id))}


@router.get("/repos/{owner}/{repo}/raw/{path:path}")
async def proxy_raw(
    owner: str,
    repo: str,
    path: str,
    ref: str = "main",
    tenant: str | None = None,
):
    """Proxy a raw file from a profile-allowlisted private repo, injecting the
    Orbital GitHub token. The tenant never sees the token. Domain-gated."""
    classify_tenant(tenant)

    repo_full = f"{owner}/{repo}".lower()
    if repo_full not in _allowed_repos():
        raise HTTPException(403, "Repository not in any profile.")
    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "Invalid path.")
    # Slashes are legal in refs (branch names like feat/my-lab); only traversal
    # and absolute/empty refs are rejected.
    if not ref or ".." in ref or ref.startswith("/"):
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
    # Pass content through as-is (markdown / yaml / images / etc). A full-sha ref is
    # immutable → let browsers cache lab images indefinitely (one fetch per student).
    from fastapi.responses import Response
    headers = {}
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return Response(content=r.content, media_type=r.headers.get("content-type", "text/plain"),
                    headers=headers)


# ── Content packs ───────────────────────────────────────────────────────────────
# A pack is the text-file bundle a training import needs (mkdocs, devcontainer,
# docs/**/*.md, assessments/**/*.json), built ONCE per commit sha from a single
# GitHub tarball download and cached on disk. Tenants that import the same repo
# hit the cache — the per-file GitHub round-trips (~15/repo, serial) disappear.
# Images are NOT packed; they stay on the raw proxy / raw.githubusercontent.

PACKS_DIR = Path(os.environ.get("CONTENT_PACKS_DIR", "/home/ops/content-packs"))
PACK_FILE_MAX = 2 * 1024 * 1024      # skip single files larger than 2 MB
PACK_TOTAL_MAX = 25 * 1024 * 1024    # abort packs larger than 25 MB of text
PACK_KEEP_PER_REPO = 2               # GC: keep this many newest packs per repo

_PACK_LOCKS: dict[str, asyncio.Lock] = {}


def _pack_wanted(path: str) -> bool:
    """Files a training import reads. Keep in sync with the app's import-lab."""
    if path in ("mkdocs.yml", "mkdocs.yaml", "enablement.yaml", "enablement.yml",
                ".devcontainer/devcontainer.json"):
        return True
    if path.startswith("docs/") and path.endswith(".md"):
        return True
    # Lab-repo assessments live in `.assessment/`; standalone scenario repos use `assessments/`.
    if path.startswith((".assessment/", "assessments/")) and path.endswith(".json"):
        return True
    return False


def _extract_pack_files(tarball: bytes) -> dict[str, str]:
    """Extract wanted text files from a GitHub tarball. GitHub prefixes every
    member with '{repo}-{sha}/', which we strip."""
    files: dict[str, str] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile() or member.size > PACK_FILE_MAX:
                continue
            path = member.name.partition("/")[2]  # strip the tarball root dir
            if not path or not _pack_wanted(path):
                continue
            total += member.size
            if total > PACK_TOTAL_MAX:
                raise HTTPException(502, "Pack too large.")
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                files[path] = fh.read().decode("utf-8")
            except UnicodeDecodeError:
                log.warning("Pack: skipping non-utf8 file %s", path)
    return files


def _pack_path(owner: str, repo: str, sha: str) -> Path:
    return PACKS_DIR / f"{owner}__{repo}__{sha}.json"


def _pack_gc(owner: str, repo: str) -> None:
    packs = sorted(PACKS_DIR.glob(f"{owner}__{repo}__*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in packs[PACK_KEEP_PER_REPO:]:
        try:
            old.unlink()
        except OSError:  # pragma: no cover - defensive
            pass


async def _build_pack(owner: str, repo: str, sha: str) -> dict:
    """Download the repo tarball at `sha` and build the pack dict. Cached on disk;
    concurrent builders for the same key coalesce on an in-process lock."""
    path = _pack_path(owner, repo, sha)
    if path.is_file():
        return json.loads(path.read_text())
    lock = _PACK_LOCKS.setdefault(f"{owner}/{repo}@{sha}", asyncio.Lock())
    async with lock:
        if path.is_file():  # built while we waited
            return json.loads(path.read_text())
        headers = {"Accept": "application/vnd.github+json"}
        if GH_TOKEN:
            headers["Authorization"] = f"Bearer {GH_TOKEN}"
        url = f"{GH_API}/repos/{owner}/{repo}/tarball/{sha}"
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(url, headers=headers)
        except Exception as exc:
            raise HTTPException(502, f"Tarball fetch failed: {exc}")
        if r.status_code == 404:
            raise HTTPException(404, "Repo or ref not found.")
        if r.status_code >= 400:
            raise HTTPException(502, f"Tarball upstream {r.status_code}.")
        pack = {
            "repo": f"{owner}/{repo}",
            "sha": sha,
            "builtAt": datetime.now(timezone.utc).isoformat(),
            "files": _extract_pack_files(r.content),
        }
        PACKS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pack))
        tmp.replace(path)  # atomic — readers never see a half-written pack
        _pack_gc(owner, repo)
        log.info("Pack built: %s/%s@%s (%d files)", owner, repo, sha[:12], len(pack["files"]))
        return pack


async def _resolve_pack_sha(owner: str, repo: str, ref: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    sha = await _latest_sha(owner, repo, ref)
    if not sha:
        raise HTTPException(404, f"Cannot resolve ref '{ref}'.")
    return sha


@router.get("/packs/{owner}/{repo}")
async def get_pack(owner: str, repo: str, ref: str = "main", tenant: str | None = None):
    """Return the import bundle for a repo at a ref (branch or full sha).
    Tenant-domain gated + profile allowlist, like the raw proxy."""
    classify_tenant(tenant)
    repo_full = f"{owner}/{repo}".lower()
    if repo_full not in _allowed_repos():
        raise HTTPException(403, "Repository not in any profile.")
    if not ref or ".." in ref or ref.startswith("/"):
        raise HTTPException(400, "Invalid ref.")
    sha = await _resolve_pack_sha(owner, repo, ref)
    return await _build_pack(owner, repo, sha)


@router.post("/packs/build")
async def build_pack_internal(request: Request):
    """Eager pack build, called by the webhook server on git push (localhost only).
    Body: {"repo": "owner/repo", "branch": "main", "sha": "<head sha>"}."""
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Internal endpoint.")
    body = await request.json()
    repo_full = str(body.get("repo", "")).lower()
    branch = str(body.get("branch") or "main")
    sha = str(body.get("sha") or "")
    owner, _, repo = repo_full.partition("/")
    if not owner or not repo:
        raise HTTPException(400, "Invalid repo.")
    if repo_full not in _allowed_repos():
        return {"status": "ignored", "reason": "repo not in any profile"}
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sha = await _resolve_pack_sha(owner, repo, branch)
    # Seed the sha cache so the next manifest reflects the push immediately.
    _SHA_CACHE[(owner, repo, branch)] = (sha, time.monotonic())
    pack = await _build_pack(owner, repo, sha)
    return {"status": "built", "repo": repo_full, "sha": sha, "files": len(pack["files"])}


# ── Profile management (writer-gated; org members only, via nginx X-Auth-User) ──

@router.get("/admin/profiles")
async def list_profiles(x_auth_user: str | None = Header(default=None)):
    """List all profiles (id, description, sources) for the management UI."""
    _require_writer(x_auth_user)
    return {"profiles": _read_profiles()}


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
    # Reporting {"ok": true} for a profile that is still listed is how "the SE demo
    # profile cannot be deleted" looked like a UI bug for a day: the console said
    # deleted, refreshed, and showed it again. If there is nothing to remove, say so.
    if not path.is_file():
        raise HTTPException(404, f"No profile '{profile_id}' on disk.")
    path.unlink()
    log.info("Profile '%s' deleted by %s", profile_id, x_auth_user)
    return {"ok": True}


# ── Tenant → profile mapping (the delivery table; writer-gated) ──────────────

@router.get("/admin/overview")
async def admin_overview(x_auth_user: str | None = Header(default=None)):
    """One call for the content console: all profiles, the delivery table, domain classes, and
    a repo catalog (union of repos across profiles) for the profile picker."""
    _require_writer(x_auth_user)
    profiles = _read_profiles()
    # Catalog = unique sources across all profiles (repo → category/label/branch), for the picker.
    catalog: dict[str, dict] = {}
    for p in profiles:
        for s in p.get("sources", []):
            if s.get("repo") and s["repo"] not in catalog:
                catalog[s["repo"]] = {"repo": s["repo"], "category": s.get("category", "uncategorized"),
                                      "categoryLabel": s.get("categoryLabel", ""), "branch": s.get("branch", "main")}
    return {"profiles": profiles, "map": _load_tenant_map(),
            "domains": sorted(set(DOMAIN_SUFFIXES.values())),
            "catalog": [catalog[k] for k in sorted(catalog)]}


@router.get("/admin/tenant-map")
async def get_tenant_map(x_auth_user: str | None = Header(default=None)):
    """The delivery table: per-domain defaults + per-tenant overrides, plus the
    available profile ids and recognised domain classes for the editor."""
    _require_writer(x_auth_user)
    profiles = sorted(p.stem for p in PROFILES_DIR.glob("*.json")) if PROFILES_DIR.is_dir() else []
    return {"map": _load_tenant_map(), "profiles": profiles, "domains": sorted(set(DOMAIN_SUFFIXES.values()))}


@router.put("/admin/tenant-map")
async def put_tenant_map(body: dict, x_auth_user: str | None = Header(default=None)):
    """Replace the tenant→profile table. Validates referenced profiles exist."""
    _require_writer(x_auth_user)
    defaults = body.get("defaults") or {}
    tenants = body.get("tenants") or {}
    if not isinstance(defaults, dict) or not isinstance(tenants, dict):
        raise HTTPException(400, "Expected {defaults:{}, tenants:{}}.")
    known = {p.stem for p in PROFILES_DIR.glob("*.json")} if PROFILES_DIR.is_dir() else set()
    for who, pid in [*defaults.items(), *tenants.items()]:
        if pid and pid not in known:
            raise HTTPException(400, f"Unknown profile '{pid}' for '{who}'.")
    clean = {
        "defaults": {str(k): str(v) for k, v in defaults.items() if v},
        "tenants": {str(k): str(v) for k, v in tenants.items() if v},
    }
    TENANT_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    TENANT_MAP_FILE.write_text(json.dumps(clean, indent=2) + "\n")
    log.info("Tenant map saved by %s (%d tenant override(s))", x_auth_user, len(clean["tenants"]))
    return {"ok": True, "tenants": len(clean["tenants"])}


@router.get("/admin/sources")
async def list_sources(x_auth_user: str | None = Header(default=None)):
    """List the managed training sources (the Trainings tab)."""
    _require_writer(x_auth_user)
    return {"sources": _load_sources()}


@router.post("/admin/validate-repo")
async def validate_repo(body: dict, x_auth_user: str | None = Header(default=None)):
    """Validate a repo URL without adding it (so the UI can show ✓/✗ before Add)."""
    _require_writer(x_auth_user)
    repo_full, url_branch = _parse_repo_ref(body.get("repo", ""))
    if not repo_full:
        raise HTTPException(400, "Provide a GitHub repo URL or owner/repo.")
    branch = (body.get("branch") or "").strip() or url_branch or "main"
    result = await _validate_repo(repo_full, branch)
    return {"repo": repo_full, "branch": branch, **result}


@router.post("/admin/sources")
async def add_source(body: dict, x_auth_user: str | None = Header(default=None)):
    """Validate a repo and add it to the managed catalog. Stored only if validation passes,
    so an invalid/unreachable repo never enters delivery."""
    _require_writer(x_auth_user)
    repo_full, url_branch = _parse_repo_ref(body.get("repo", ""))
    if not repo_full:
        raise HTTPException(400, "Provide a GitHub repo URL or owner/repo.")
    # Branch precedence: explicit field → /tree/{branch} in the pasted URL → main.
    branch = (body.get("branch") or "").strip() or url_branch or "main"
    result = await _validate_repo(repo_full, branch)
    if not result.get("valid"):
        raise HTTPException(400, result.get("reason", "Repository did not validate."))
    sources = _load_sources()
    existing = next((s for s in sources if s.get("repo", "").lower() == repo_full.lower()), None)
    if existing:
        # Re-adding a managed repo with a different branch switches its branch —
        # this is how a training is flipped to a test branch and back to main.
        if (existing.get("branch") or "main") != branch:
            existing["branch"] = branch
            _save_sources(sources)
            log.info("Switched managed source %s to branch %s by %s", repo_full, branch, x_auth_user)
            return {"ok": True, "source": existing, "branchSwitched": True}
        raise HTTPException(409, f"{repo_full} is already managed.")
    category = (body.get("category") or "hands-on").strip()
    entry = {
        "repo": repo_full,
        "branch": branch or result.get("defaultBranch", "main"),
        "category": category,
        "categoryLabel": (body.get("categoryLabel") or "").strip() or category.replace("-", " ").title(),
        "delivery": result.get("delivery", "self-paced"),
        "private": bool(body.get("private", False)),
        "addedBy": x_auth_user or "",
    }
    sources.append(entry)
    _save_sources(sources)
    log.info("Added managed source %s (%s) by %s", repo_full, entry["delivery"], x_auth_user)
    return {"ok": True, "source": entry}


@router.delete("/admin/sources/{owner}/{repo}")
async def remove_source(owner: str, repo: str, x_auth_user: str | None = Header(default=None)):
    """Remove a repo from the managed catalog. Does not touch profiles that reference it."""
    _require_writer(x_auth_user)
    repo_full = f"{owner}/{repo}".lower()
    sources = _load_sources()
    kept = [s for s in sources if s.get("repo", "").lower() != repo_full]
    if len(kept) == len(sources):
        raise HTTPException(404, f"{owner}/{repo} is not a managed source.")
    _save_sources(kept)
    log.info("Removed managed source %s/%s by %s", owner, repo, x_auth_user)
    return {"ok": True, "removed": f"{owner}/{repo}"}


@router.post("/admin/register-tenant")
async def register_tenant(body: dict, x_auth_user: str | None = Header(default=None)):
    """Auto-register a tenant when the app is deployed there, so its content can be
    managed. Adds the tenant with its per-domain default profile if not already present
    (an existing override is left untouched). Returns the resolved profile."""
    _require_writer(x_auth_user)
    tenant_id, domain_class = classify_tenant(body.get("tenant"))
    m = _load_tenant_map()
    tenants = m.setdefault("tenants", {})
    added = tenant_id not in tenants
    if added:
        tenants[tenant_id] = m.get("defaults", {}).get(domain_class, "all")
        TENANT_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        TENANT_MAP_FILE.write_text(json.dumps(m, indent=2) + "\n")
        log.info("Registered tenant %s (%s) → profile %s by %s", tenant_id, domain_class, tenants[tenant_id], x_auth_user)
    return {"ok": True, "tenant": tenant_id, "domain": domain_class, "profile": tenants[tenant_id], "added": added}
