#!/usr/bin/env python3
"""coverage_scan.py — grade every training on how much of it a machine can drive.

"Is this training fully automated?" has been an invisible property. It decides
three things at once: whether nightly `training-test` can prove the training
still works, whether a learner can leave and resume it (resume replays the same
LAB_SOLUTION blocks), and whether the fleet view is telling the truth about what
we ship. Only 4 of 27 repos carry the annotation grammar at all, and nothing
surfaced that.

This is the cheap half of the answer: a static read of `mkdocs.yaml` + `docs/*.md`
straight from GitHub — no clone, no container, no provisioning. `training-test`
is the expensive half, and a green end-to-end run upgrades a repo from
`complete` to `verified`.

Grades:
  verified  every page owing a solution has one, and training-test passed E2E
  complete  every page owing a solution has one; no passing E2E run yet
  partial   some pages owing a solution have one
  none      no LAB_QUESTION / LAB_SOLUTION grammar at all

A page "owes a solution" when it asks the learner to *do* something — it carries
a STEP_SETUP or a shell-verification check — unless the author marked it
`<!-- LAB_NO_SOLUTION: reason -->`. That marker exists because the distinction
cannot be inferred: kubernetes-101's prerequisites page runs two shell checks
that confirm the *environment* came up, which is not work a learner can solve.

Usage:
  coverage_scan.py [--repos repos.yaml] [--only NAME] [--json] [--no-redis]
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app_layer_driver import BLOCK_RE, parse_block  # noqa: E402
from lab_nav import build_training_groups, page_is_exempt  # noqa: E402

RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
TIMEOUT_S = 20
GRADES = ("verified", "complete", "partial", "none")


def _fetch(repo, ref, path, token=""):
    url = RAW.format(repo=repo, ref=ref, path=path)
    req = urllib.request.Request(url, headers={"User-Agent": "orbital-coverage-scan"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except Exception:
        return None


def _github_token():
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def load_repos(path):
    """Minimal reader for repos.yaml — name/repo/tags only, no pyyaml needed."""
    entries, current = [], None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^  - name:\s*(\S+)", line)
            if m:
                if current:
                    entries.append(current)
                current = {"name": m.group(1), "repo": "", "tags": []}
                continue
            if current is None:
                continue
            m = re.match(r"^    repo:\s*(\S+)", line)
            if m:
                current["repo"] = m.group(1)
            m = re.match(r"^    tags:\s*\[(.*)\]", line)
            if m:
                current["tags"] = [t.strip() for t in m.group(1).split(",") if t.strip()]
    if current:
        entries.append(current)
    return entries


def page_facts(text):
    """(owes_solution, has_solution) for one page."""
    has_solution = False
    hands_on = False
    for kind, body in BLOCK_RE.findall(text):
        if kind == "LAB_SOLUTION":
            try:
                doc = parse_block(body)
            except Exception:
                continue
            if isinstance(doc, dict) and (doc.get("commands") or doc.get("verify")):
                has_solution = True
        elif kind == "STEP_SETUP":
            hands_on = True
        elif kind == "LAB_QUESTION":
            try:
                doc = parse_block(body)
            except Exception:
                continue
            if isinstance(doc, dict) and doc.get("type") == "shell-verification":
                hands_on = True
    return hands_on, has_solution


def scan_repo(entry, ref="main", token=""):
    repo = entry["repo"]
    result = {
        "name": entry["name"], "repo": repo, "ref": ref,
        "grade": "none", "owed": 0, "covered": 0, "exempt": 0,
        "gaps": [], "pages": 0, "error": "",
    }

    mkdocs = _fetch(repo, ref, "mkdocs.yaml", token) or _fetch(repo, ref, "mkdocs.yml", token)
    if mkdocs is None:
        result["error"] = "no mkdocs config"
        return result

    try:
        groups = build_training_groups(mkdocs)
    except Exception as exc:
        result["error"] = f"nav parse failed: {exc}"
        return result

    # A packed repo ships several trainings; grade the repo on all of them,
    # since the fleet view is per-repo and nightly tests the repo as a whole.
    seen = []
    for group in groups:
        for entry_nav in group.entries:
            if entry_nav.filename not in seen:
                seen.append(entry_nav.filename)

    owed = covered = exempt = 0
    gaps = []
    for filename in seen:
        text = _fetch(repo, ref, f"docs/{filename}", token)
        if text is None:
            continue
        result["pages"] += 1
        is_exempt, _reason = page_is_exempt(text)
        hands_on, has_solution = page_facts(text)
        if is_exempt:
            exempt += 1
            continue
        if not hands_on:
            continue
        owed += 1
        if has_solution:
            covered += 1
        else:
            gaps.append(filename)

    result.update(owed=owed, covered=covered, exempt=exempt, gaps=gaps)
    if owed == 0 and covered == 0 and exempt == 0:
        result["grade"] = "none"
    elif gaps:
        result["grade"] = "partial"
    else:
        result["grade"] = "complete"
    return result


def write_redis(results):
    """Publish grades for the fleet view. Best-effort — scanning is still useful
    without Redis (that is what --json is for)."""
    try:
        import redis  # noqa: PLC0415
    except ImportError:
        print("redis module unavailable — skipping publish", file=sys.stderr)
        return False
    pwd = os.environ.get("REDIS_PASSWORD", "")
    try:
        client = redis.Redis(host="127.0.0.1", port=6379, password=pwd or None, decode_responses=True)
        pipe = client.pipeline()
        for r in results:
            key = f"training:coverage:{r['name']}"
            # Never downgrade a `verified` grade from a static scan: only
            # training-test can grant it, and only a real E2E run can revoke it.
            existing = client.hget(key, "grade")
            grade = r["grade"]
            if existing == "verified" and grade == "complete":
                grade = "verified"
            pipe.hset(key, mapping={
                "grade": grade, "owed": r["owed"], "covered": r["covered"],
                "exempt": r["exempt"], "gaps": json.dumps(r["gaps"]),
                "pages": r["pages"], "ref": r["ref"], "repo": r["repo"],
                "scannedAt": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).isoformat(),
            })
            pipe.expire(key, 30 * 24 * 3600)
            pipe.sadd("training:coverage:index", r["name"])
        pipe.execute()
        return True
    except Exception as exc:
        print(f"redis publish failed: {exc}", file=sys.stderr)
        return False


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Grade trainings on automation coverage")
    ap.add_argument("--repos", default=os.path.join(os.path.dirname(here), "repos.yaml"))
    ap.add_argument("--only", default="", help="scan a single repo by name")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    ap.add_argument("--no-redis", action="store_true", help="do not publish to Redis")
    args = ap.parse_args()

    entries = load_repos(args.repos)
    # Infrastructure repos are not trainings and have no docs/ to grade.
    skip = {"codespaces-framework", "dynatrace-wwse.github.io", "codespaces-tracker"}
    entries = [e for e in entries if e["name"] not in skip]
    if args.only:
        entries = [e for e in entries if e["name"] == args.only]
    if not entries:
        print("no repos to scan", file=sys.stderr)
        return 2

    token = _github_token()
    results = [scan_repo(e, args.ref, token) for e in entries]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        width = max(len(r["name"]) for r in results)
        for r in sorted(results, key=lambda x: (GRADES.index(x["grade"]), x["name"])):
            detail = f"{r['covered']}/{r['owed']} covered, {r['exempt']} exempt"
            gaps = f"  gaps: {', '.join(r['gaps'])}" if r["gaps"] else ""
            err = f"  ({r['error']})" if r["error"] else ""
            print(f"{r['name']:<{width}}  {r['grade']:<9} {detail}{gaps}{err}")
        by_grade = {g: sum(1 for r in results if r["grade"] == g) for g in GRADES}
        print("\n" + "  ".join(f"{g}={by_grade[g]}" for g in GRADES))

    if not args.no_redis:
        write_redis(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
