#!/usr/bin/env python3
"""Phase 1: Static analysis — Gen2 vs Gen3 keyword scan across all repo documentation."""

import json
import os
import re
import yaml
from collections import defaultdict

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REPOS_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repos.yaml")
OUTPUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase1-results.json")

# Load repos
with open(REPOS_YAML) as f:
    repos_yaml = yaml.safe_load(f)
REPOS = [r["name"] for r in repos_yaml.get("repos", [])]

# ── Keyword definitions ──

GEN2_KEYWORDS = {
    # Classic charting/analytics
    "Data Explorer": re.compile(r"Data\s+Explorer", re.IGNORECASE),
    "Create custom chart": re.compile(r"Create\s+custom\s+chart", re.IGNORECASE),
    "Multidimensional analysis": re.compile(r"Multidimensional\s+analysis", re.IGNORECASE),
    "Classic UI": re.compile(r"Classic\s+(UI|view|interface)", re.IGNORECASE),
    # Classic entity/query
    "entity selector": re.compile(r"entity\s*selector", re.IGNORECASE),
    "Management Zone": re.compile(r"Management\s+Zone", re.IGNORECASE),
    # Classic navigation paths
    "Observe and explore": re.compile(r"Observe\s+and\s+explore", re.IGNORECASE),
    "Transactions & services": re.compile(r"Transactions\s*[&and]+\s*services", re.IGNORECASE),
    "Technologies and processes": re.compile(r"Technologies(\s+and\s+processes)?(?=\s*[>)]|\s+menu|\s+section)", re.IGNORECASE),
    "Hosts (classic menu)": re.compile(r"(?:navigate|go)\s+to\s+Hosts\b|Hosts\s*>|Infrastructure\s*>\s*Hosts", re.IGNORECASE),
    # Classic settings
    "Settings >": re.compile(r"Settings\s*>", re.IGNORECASE),
    "Application detection": re.compile(r"Application\s+detection", re.IGNORECASE),
    # Classic entity types
    "Web application": re.compile(r"Web\s+application(?!s?\s+monitoring)", re.IGNORECASE),
    "Process group": re.compile(r"Process\s+group", re.IGNORECASE),
    "Host group": re.compile(r"Host\s+group", re.IGNORECASE),
    "custom device": re.compile(r"custom\s+device", re.IGNORECASE),
    # Classic features
    "Deployment status": re.compile(r"Deployment\s+status", re.IGNORECASE),
    "Smartscape (classic)": re.compile(r"Smartscape", re.IGNORECASE),
    "Synthetic (menu)": re.compile(r"(?:navigate|go)\s+to\s+Synthetic|Synthetic\s*>|Synthetic\s+monitoring", re.IGNORECASE),
    "RUM (classic menu)": re.compile(r"Real\s+User\s+Monitoring|(?:navigate|go)\s+to\s+RUM", re.IGNORECASE),
    # Classic API patterns
    "v1 API": re.compile(r"/api/v1/", re.IGNORECASE),
    "Timeseries API": re.compile(r"timeseries\s+API|/api/v1/timeseries", re.IGNORECASE),
}

GEN3_KEYWORDS = {
    "Notebooks": re.compile(r"\bNotebooks?\b(?!\s*\()"),  # Avoid matching code refs
    "Grail": re.compile(r"\bGrail\b"),
    "DQL": re.compile(r"\bDQL\b"),
    "Apps >": re.compile(r"Apps\s*>|open\s+the\s+\w+\s+app\b", re.IGNORECASE),
    "OpenPipeline": re.compile(r"OpenPipeline", re.IGNORECASE),
    "Ownership": re.compile(r"\bOwnership\b(?!\s+team)", re.IGNORECASE),
    "Davis CoPilot": re.compile(r"Davis\s+(CoPilot|AI)", re.IGNORECASE),
    "Workflows": re.compile(r"\bWorkflows?\b(?=\s+app|\s+automation|\s*>)", re.IGNORECASE),
    "Automations": re.compile(r"\bAutomations?\b", re.IGNORECASE),
    "Launcher": re.compile(r"\bLauncher\b", re.IGNORECASE),
    "Hub": re.compile(r"\bHub\b(?=\s+app|\s+market|\s*>)", re.IGNORECASE),
    "Segments": re.compile(r"\bSegments?\b(?=\s+in|\s+for|\s+to|\s+config)", re.IGNORECASE),
    "Buckets": re.compile(r"\bBuckets?\b(?=\s+in|\s+for|\s+to|\s+config|\s+storage)", re.IGNORECASE),
    "fetchLogs": re.compile(r"fetch\s+logs|fetch\s+dt\.logs", re.IGNORECASE),
    "fetch spans": re.compile(r"fetch\s+spans|fetch\s+dt\.spans", re.IGNORECASE),
    "fetch events": re.compile(r"fetch\s+events|fetch\s+dt\.events|fetch\s+bizevents", re.IGNORECASE),
    "v2 API": re.compile(r"/api/v2/", re.IGNORECASE),
}


def scan_file(content, keywords):
    """Scan file content for keywords. Returns {keyword: [{line: N, context: "..."}]}"""
    hits = defaultdict(list)
    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        for kw_name, pattern in keywords.items():
            if pattern.search(line):
                hits[kw_name].append({
                    "line": i,
                    "context": line.strip()[:150],
                })
    return dict(hits)


def count_images_in_md(content):
    """Count image references in markdown content."""
    return len(re.findall(r'!\[.*?\]\(.*?\)', content))


def classify_risk(gen2_count, gen3_count):
    """Classify doc risk level."""
    if gen2_count == 0:
        return "GREEN"
    if gen2_count > 5:
        return "RED"
    if gen3_count > 0:
        return "YELLOW"
    return "RED"


# ── Main scan ──

results = {"repos": {}, "summary": {}}

for repo in REPOS:
    docs_dir = os.path.join(DATA_DIR, repo, "docs")

    # Read counts
    try:
        doc_count = int(open(os.path.join(DATA_DIR, repo, "doc_count.txt")).read().strip())
    except:
        doc_count = 0
    try:
        img_count = int(open(os.path.join(DATA_DIR, repo, "img_count.txt")).read().strip())
    except:
        img_count = 0

    repo_result = {
        "doc_count": doc_count,
        "screenshot_count": img_count,
        "gen2_hits": {},
        "gen3_hits": {},
        "gen2_count": 0,
        "gen3_count": 0,
        "risk": "N/A",
        "files": {},
        "gen2_keywords_found": [],
        "gen3_keywords_found": [],
        "red_files": [],
        "yellow_files": [],
        "green_files": [],
    }

    if not os.path.isdir(docs_dir) or doc_count == 0:
        results["repos"][repo] = repo_result
        # Write per-repo phase1.json
        with open(os.path.join(DATA_DIR, repo, "phase1.json"), "w") as f:
            json.dump(repo_result, f, indent=2)
        continue

    # Scan all .md files
    all_gen2_hits = defaultdict(list)
    all_gen3_hits = defaultdict(list)
    total_gen2 = 0
    total_gen3 = 0

    for root, dirs, files in os.walk(docs_dir):
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, docs_dir)

            try:
                content = open(fpath).read()
            except:
                continue

            file_gen2 = scan_file(content, GEN2_KEYWORDS)
            file_gen3 = scan_file(content, GEN3_KEYWORDS)
            file_images = count_images_in_md(content)

            file_gen2_count = sum(len(v) for v in file_gen2.values())
            file_gen3_count = sum(len(v) for v in file_gen3.values())
            file_risk = classify_risk(file_gen2_count, file_gen3_count)

            repo_result["files"][rel_path] = {
                "gen2_count": file_gen2_count,
                "gen3_count": file_gen3_count,
                "risk": file_risk,
                "images": file_images,
                "gen2_keywords": list(file_gen2.keys()),
                "gen3_keywords": list(file_gen3.keys()),
            }

            if file_risk == "RED":
                repo_result["red_files"].append(rel_path)
            elif file_risk == "YELLOW":
                repo_result["yellow_files"].append(rel_path)
            else:
                repo_result["green_files"].append(rel_path)

            total_gen2 += file_gen2_count
            total_gen3 += file_gen3_count

            for kw, hits in file_gen2.items():
                for hit in hits:
                    hit["file"] = rel_path
                    all_gen2_hits[kw].append(hit)

            for kw, hits in file_gen3.items():
                for hit in hits:
                    hit["file"] = rel_path
                    all_gen3_hits[kw].append(hit)

    repo_result["gen2_hits"] = dict(all_gen2_hits)
    repo_result["gen3_hits"] = dict(all_gen3_hits)
    repo_result["gen2_count"] = total_gen2
    repo_result["gen3_count"] = total_gen3
    repo_result["risk"] = classify_risk(total_gen2, total_gen3)
    repo_result["gen2_keywords_found"] = sorted(all_gen2_hits.keys())
    repo_result["gen3_keywords_found"] = sorted(all_gen3_hits.keys())

    results["repos"][repo] = repo_result

    # Write per-repo phase1.json for the master table generator
    phase1_summary = {
        "doc_count": repo_result["doc_count"],
        "screenshot_count": repo_result["screenshot_count"],
        "gen2_count": repo_result["gen2_count"],
        "gen3_count": repo_result["gen3_count"],
        "risk": repo_result["risk"],
        "gen2_keywords_found": repo_result["gen2_keywords_found"],
        "gen3_keywords_found": repo_result["gen3_keywords_found"],
        "red_files": repo_result["red_files"],
        "yellow_files": repo_result["yellow_files"],
    }
    with open(os.path.join(DATA_DIR, repo, "phase1.json"), "w") as f:
        json.dump(phase1_summary, f, indent=2)

# ── Summary stats ──
total_repos = len(REPOS)
red_repos = [r for r in REPOS if results["repos"][r]["risk"] == "RED"]
yellow_repos = [r for r in REPOS if results["repos"][r]["risk"] == "YELLOW"]
green_repos = [r for r in REPOS if results["repos"][r]["risk"] == "GREEN"]
na_repos = [r for r in REPOS if results["repos"][r]["risk"] == "N/A"]

results["summary"] = {
    "total_repos": total_repos,
    "total_docs": sum(results["repos"][r]["doc_count"] for r in REPOS),
    "total_screenshots": sum(results["repos"][r]["screenshot_count"] for r in REPOS),
    "red_repos": len(red_repos),
    "yellow_repos": len(yellow_repos),
    "green_repos": len(green_repos),
    "na_repos": len(na_repos),
    "red_repo_names": red_repos,
    "yellow_repo_names": yellow_repos,
    "green_repo_names": green_repos,
}

# Write full results
with open(OUTPUT_JSON, "w") as f:
    json.dump(results, f, indent=2)

# ── Print report ──
print(f"\n{'='*60}")
print(f"  PHASE 1 RESULTS — Gen2/Gen3 Documentation Scan")
print(f"{'='*60}")
print(f"  Total repos scanned:  {total_repos}")
print(f"  Total doc pages:      {results['summary']['total_docs']}")
print(f"  Total screenshots:    {results['summary']['total_screenshots']}")
print(f"")
print(f"  RED   (Gen2 heavy):   {len(red_repos)}")
for r in red_repos:
    g2 = results["repos"][r]["gen2_count"]
    g3 = results["repos"][r]["gen3_count"]
    kw = ", ".join(results["repos"][r]["gen2_keywords_found"][:5])
    print(f"    - {r} (gen2:{g2} gen3:{g3}) [{kw}]")

print(f"")
print(f"  YELLOW (Mixed):       {len(yellow_repos)}")
for r in yellow_repos:
    g2 = results["repos"][r]["gen2_count"]
    g3 = results["repos"][r]["gen3_count"]
    kw = ", ".join(results["repos"][r]["gen2_keywords_found"][:5])
    print(f"    - {r} (gen2:{g2} gen3:{g3}) [{kw}]")

print(f"")
print(f"  GREEN (Gen3/clean):   {len(green_repos)}")
for r in green_repos:
    print(f"    - {r}")

print(f"")
print(f"  N/A (no docs):        {len(na_repos)}")
for r in na_repos:
    print(f"    - {r}")

print(f"\n  Full results: {OUTPUT_JSON}")
print(f"  Per-repo data: audit/data/{{repo}}/phase1.json")
print(f"{'='*60}")
