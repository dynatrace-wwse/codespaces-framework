#!/usr/bin/env python3
"""Generate master audit HTML table from fetched repo data."""

import json
import json5
import os
import re
import hashlib
import yaml
from html import escape
from datetime import datetime

DATA_DIR = "/home/ubuntu/enablement-framework/codespaces-framework/audit/data"
REPOS_YAML = "/home/ubuntu/enablement-framework/codespaces-framework/repos.yaml"
OUTPUT = "/home/ubuntu/enablement-framework/codespaces-framework/audit/master-table.html"

# Load repos.yaml for extra metadata
with open(REPOS_YAML) as f:
    repos_yaml = yaml.safe_load(f)

yaml_meta = {}
for r in repos_yaml.get("repos", []):
    yaml_meta[r["name"]] = r

REPOS = [
    "enablement-kubernetes-opentelemetry",
    "enablement-gen-ai-llm-observability",
    "enablement-business-observability",
    "enablement-dql-301",
    "enablement-dynatrace-log-ingest-101",
    "enablement-browser-dem-biz-observability",
    "enablement-live-debugger-bug-hunting",
    "enablement-workflow-essentials",
    "enablement-azure-webapp-otel",
    "enablement-codespaces-template",
    "enablement-openpipeline-segments-iam",
    "enablement-kubernetes-opentelemetry-openpipeline",
    "enablement-dql-fundamentals",
    "workshop-dynatrace-log-analytics",
    "workshop-destination-automation",
    "demo-agentic-ai-with-nvidia",
    "demo-mcp-unguard",
    "demo-opentelemetry",
    "demo-astroshop-runtime-optimization",
    "demo-astroshop-problems",
    "ace-integration",
    "bizobs-journey-simulator",
    "bug-busters",
    "remote-environment",
    "codespaces-framework",
    "dynatrace-wwse.github.io",
    "codespaces-tracker",
]


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except:
        return ""


def parse_json_safe(text):
    if not text:
        return None
    # Remove problematic control characters but keep newlines/tabs
    clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', text)
    # Try json5 on raw content first (handles JSONC comments natively)
    try:
        return json5.loads(clean)
    except:
        pass
    # Strip JSONC comments manually and try standard json
    stripped = re.sub(r'//.*?$', '', clean, flags=re.MULTILINE)
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.DOTALL)
    stripped = re.sub(r',\s*([}\]])', r'\1', stripped)
    try:
        return json.loads(stripped, strict=False)
    except:
        return None


def extract_rum_config(data_dir, repo_name):
    """Extract RUM snippet info from mkdocs.yaml."""
    mkdocs_path = os.path.join(data_dir, repo_name, "mkdocs.yaml")
    try:
        with open(mkdocs_path) as f:
            d = yaml.safe_load(f)
        extra = d.get("extra", {})
        rum = extra.get("rum_snippet", "")
        inherits = d.get("INHERIT", "")
        # Extract the unique hash from the RUM URL
        rum_hash = ""
        if rum:
            parts = rum.split("/")
            for p in parts:
                if "_complete" in p:
                    rum_hash = p.replace("_complete.js", "")
                    break
        return {
            "has_rum": bool(rum),
            "rum_snippet": rum,
            "rum_hash": rum_hash,
            "inherits_base": bool(inherits),
            "has_mkdocs": True,
        }
    except:
        return {
            "has_rum": False,
            "rum_snippet": "",
            "rum_hash": "",
            "inherits_base": False,
            "has_mkdocs": False,
        }


def extract_function_calls(sh_content):
    """Extract function calls from a shell script (post-create.sh, post-start.sh).
    Returns list of function names that are called (not defined) in the script."""
    if not sh_content:
        return []
    skip_cmds = {"source", "export", "echo", "cd", "mkdir", "chmod", "cp", "mv",
                 "rm", "ln", "cat", "grep", "sed", "awk", "curl", "wget", "git",
                 "bash", "sh", "sudo", "apt", "pip", "npm", "docker", "kubectl",
                 "make", "sleep", "wait", "kill", "set", "unset", "eval", "exec",
                 "test", "true", "false", "exit", "return", "read", "printf", "local",
                 "helm", "kind", "jq", "yq", "tar", "unzip", "chmod", "chown",
                 "tee", "touch", "wc", "sort", "head", "tail", "cut", "tr",
                 "xargs", "find", "which", "whoami", "date", "env", "nohup"}
    calls = []
    for line in sh_content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        # Match lines that start with a function call (camelCase or snake_case with caps)
        m = re.match(r'^([a-zA-Z]\w*)\b', line)
        if m:
            name = m.group(1)
            if name not in skip_cmds and not name.startswith("if") and len(name) > 3:
                if any(c.isupper() for c in name[1:]) or "_" in name:
                    calls.append(name)
    return list(dict.fromkeys(calls))  # dedupe preserving order


def extract_function_definitions(sh_content):
    """Extract function definitions from my_functions.sh.
    Returns list of function names defined in the file."""
    if not sh_content:
        return []
    funcs = re.findall(r'(?:function\s+)?(\w+)\s*\(\)\s*\{', sh_content)
    skip = {"if", "while", "for", "then", "else", "fi", "do", "done", "case", "esac"}
    return [f for f in funcs if f not in skip]


def extract_env_vars(sh_content):
    """Extract environment variable references from shell content.
    Returns sorted list of unique env var names."""
    if not sh_content:
        return []
    # Match $VAR and ${VAR} patterns — uppercase with underscores
    envs = set(re.findall(r'\$\{?([A-Z][A-Z0-9_]{2,})\}?', sh_content))
    # Filter out common shell builtins
    builtins = {"SECONDS", "BASH", "SHELL", "HOME", "USER", "PATH", "PWD", "OLDPWD",
                "HOSTNAME", "RANDOM", "LINENO", "FUNCNAME", "BASH_SOURCE", "PIPESTATUS",
                "IFS", "PS1", "PS2", "TERM", "LANG", "LC_ALL", "SHLVL", "UID", "EUID",
                "GROUPS", "OSTYPE", "MACHTYPE", "HOSTTYPE", "BASH_VERSION", "EOF"}
    return sorted(envs - builtins)


def count_badges(readme):
    """Count shield.io badges or similar badge patterns in README."""
    if not readme:
        return 0
    badge_patterns = [
        r'!\[.*?\]\(https?://.*?shields\.io',
        r'!\[.*?\]\(https?://.*?badge',
        r'!\[.*?\]\(https?://.*?img\.shields',
        r'<img.*?src=["\']https?://.*?shields\.io',
        r'<img.*?src=["\']https?://.*?badge',
    ]
    count = 0
    for pattern in badge_patterns:
        count += len(re.findall(pattern, readme, re.IGNORECASE))
    return count


def summarize_command(cmd):
    """Summarize a postCreateCommand or postStartCommand."""
    if not cmd:
        return "N/A"
    if isinstance(cmd, list):
        cmd = " && ".join(cmd) if isinstance(cmd[0], str) else str(cmd)
    if isinstance(cmd, dict):
        parts = []
        for k, v in cmd.items():
            if isinstance(v, str):
                parts.append(f"{k}: {v[:60]}")
            elif isinstance(v, list):
                parts.append(f"{k}: {' '.join(str(x) for x in v)[:60]}")
        return "; ".join(parts)[:120]
    cmd = str(cmd)
    if len(cmd) > 120:
        return cmd[:117] + "..."
    return cmd


def get_docker_image(dc):
    """Extract docker image from devcontainer.json."""
    if not dc:
        return "N/A"
    # Direct image
    if "image" in dc:
        return dc["image"]
    # Build with dockerfile
    if "build" in dc:
        b = dc["build"]
        if isinstance(b, dict):
            if "dockerfile" in b:
                return f"Dockerfile: {b['dockerfile']}"
            if "image" in b:
                return b["image"]
    return "N/A"


def get_extensions(dc):
    """Get extensions list from devcontainer.json."""
    if not dc:
        return []
    exts = []
    # customizations.vscode.extensions
    customizations = dc.get("customizations", {})
    vscode = customizations.get("vscode", {})
    exts.extend(vscode.get("extensions", []))
    # Legacy: extensions key
    exts.extend(dc.get("extensions", []))
    return exts


def get_secrets(dc):
    """Find secrets references in devcontainer.json."""
    if not dc:
        return []
    secrets = []
    dc_str = json.dumps(dc)
    # Look for secrets key
    if "secrets" in dc:
        s = dc["secrets"]
        if isinstance(s, dict):
            secrets.extend(s.keys())
    # Look for remoteEnv referencing secrets
    remote_env = dc.get("remoteEnv", {})
    for k, v in remote_env.items():
        if isinstance(v, str) and ("secret" in v.lower() or "${localEnv:" in v or "CODESPACE_" in v):
            secrets.append(k)
    # containerEnv
    container_env = dc.get("containerEnv", {})
    for k, v in container_env.items():
        if isinstance(v, str) and "secret" in v.lower():
            secrets.append(k)
    return list(set(secrets))


def get_remote_env_keys(dc):
    if not dc:
        return []
    return list(dc.get("remoteEnv", {}).keys())


def has_ports_config(dc):
    if not dc:
        return False
    return "portsAttributes" in dc or "forwardPorts" in dc


def get_ports_summary(dc):
    if not dc:
        return "N/A"
    parts = []
    if "forwardPorts" in dc:
        parts.append(f"fwd:{dc['forwardPorts']}")
    if "portsAttributes" in dc:
        ports = list(dc["portsAttributes"].keys())
        parts.append(f"attr:{ports}")
    if not parts:
        return "N/A"
    s = "; ".join(str(p) for p in parts)
    return s[:100] if len(s) > 100 else s


# Collect all data
all_rows = []
all_rum_hashes = {}  # rum_hash -> list of repos
all_remote_envs = {}  # hash -> list of repos

for repo in REPOS:
    rdir = os.path.join(DATA_DIR, repo)
    meta_text = read_file(os.path.join(rdir, "meta.json"))
    meta = parse_json_safe(meta_text) or {}
    contributors = read_file(os.path.join(rdir, "contributors.txt")) or "0"
    dc_text = read_file(os.path.join(rdir, "devcontainer.json"))
    dc = parse_json_safe(dc_text)
    myfunc_content = read_file(os.path.join(rdir, "my_functions.sh"))
    postcreate_content = read_file(os.path.join(rdir, "post-create.sh"))
    poststart_content = read_file(os.path.join(rdir, "post-start.sh"))
    readme = read_file(os.path.join(rdir, "README.md"))

    # RUM config from mkdocs.yaml
    rum_info = extract_rum_config(DATA_DIR, repo)

    # Phase 1 doc validation results
    phase1_path = os.path.join(rdir, "phase1.json")
    phase1 = parse_json_safe(read_file(phase1_path)) or {}

    # YAML metadata
    ym = yaml_meta.get(repo, {})

    row = {
        "name": repo,
        "created": meta.get("created_at", "N/A")[:10] if meta.get("created_at") else "N/A",
        "fork": meta.get("fork", False),
        "contributors": contributors,
        "docker_image": get_docker_image(dc),
        "extensions": get_extensions(dc),
        "extensions_count": len(get_extensions(dc)),
        "secrets": get_secrets(dc),
        "post_create_calls": extract_function_calls(postcreate_content),
        "post_start_calls": extract_function_calls(poststart_content),
        "my_function_defs": extract_function_definitions(myfunc_content),
        "other_envs": sorted(set(
            extract_env_vars(postcreate_content) +
            extract_env_vars(poststart_content) +
            extract_env_vars(myfunc_content)
        )),
        "readme_badges": count_badges(readme),
        "readme_standard": count_badges(readme) > 0,
        "has_readme": bool(readme),
        "remote_env_keys": get_remote_env_keys(dc),
        "has_ports": has_ports_config(dc),
        "ports_summary": get_ports_summary(dc),
        "has_mkdocs": rum_info["has_mkdocs"],
        "has_rum": rum_info["has_rum"],
        "rum_snippet": rum_info["rum_snippet"],
        "rum_hash": rum_info["rum_hash"],
        "inherits_base": rum_info["inherits_base"],
        "has_devcontainer": bool(dc),
        # Phase 1: Doc validation
        "doc_count": phase1.get("doc_count", 0),
        "screenshot_count": phase1.get("screenshot_count", 0),
        "gen2_count": phase1.get("gen2_count", 0),
        "gen3_count": phase1.get("gen3_count", 0),
        "doc_risk": phase1.get("risk", "N/A"),
        "gen2_keywords": phase1.get("gen2_keywords_found", []),
        "gen3_keywords": phase1.get("gen3_keywords_found", []),
        "red_files": phase1.get("red_files", []),
        "yellow_files": phase1.get("yellow_files", []),
        "sync_managed": ym.get("sync_managed", True),
        "status": ym.get("status", "N/A"),
        "tags": ym.get("tags", []),
        "dc_raw": dc,
    }

    # Track RUM hash for uniqueness
    if rum_info["rum_hash"]:
        h = rum_info["rum_hash"]
        all_rum_hashes.setdefault(h, []).append(repo)

    re_keys = tuple(sorted(row["remote_env_keys"]))
    if re_keys:
        h = hashlib.md5(str(re_keys).encode()).hexdigest()
        all_remote_envs.setdefault(h, []).append(repo)

    all_rows.append(row)

# Determine RUM snippet uniqueness
for row in all_rows:
    if row["rum_hash"]:
        h = row["rum_hash"]
        if len(all_rum_hashes.get(h, [])) == 1:
            row["rum_unique"] = "Unique"
        else:
            others = [r for r in all_rum_hashes[h] if r != row["name"]]
            row["rum_unique"] = f"Shared ({len(others)+1})"
            row["rum_shared_with"] = others
    else:
        row["rum_unique"] = "N/A"
        row["rum_shared_with"] = []

# Determine remoteEnv uniqueness
for row in all_rows:
    re_keys = tuple(sorted(row["remote_env_keys"]))
    if re_keys:
        h = hashlib.md5(str(re_keys).encode()).hexdigest()
        if len(all_remote_envs.get(h, [])) == 1:
            row["remote_env_unique"] = "Unique"
        else:
            row["remote_env_unique"] = f"Shared ({len(all_remote_envs[h])})"
    else:
        row["remote_env_unique"] = "N/A"

# Determine image_tier from docker image
for row in all_rows:
    img = row["docker_image"]
    if "N/A" == img:
        row["image_tier"] = "N/A"
    elif "large" in img.lower():
        row["image_tier"] = "large"
    elif "base" in img.lower() or "universal" in img.lower():
        row["image_tier"] = "base"
    elif "minimal" in img.lower():
        row["image_tier"] = "minimal"
    else:
        # Extract last meaningful segment
        row["image_tier"] = img.split("/")[-1].split(":")[0][:30] if "/" in img else img[:40]


# ── Generate HTML ──
def cell_class(val, good_vals=None, bad_vals=None):
    """Return a CSS class based on value."""
    if val in (None, "N/A", "Missing", "", False, 0, "0", []):
        return "missing"
    if good_vals and val in good_vals:
        return "good"
    if bad_vals and val in bad_vals:
        return "bad"
    return ""


def bool_cell(val, true_text="Yes", false_text="No"):
    if val:
        return f'<td class="good">{true_text}</td>'
    return f'<td class="missing">{false_text}</td>'


html_parts = []
html_parts.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Enablement Framework - Master Technical Audit</title>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --green: #2ea043;
    --green-bg: #0d2818;
    --red: #f85149;
    --red-bg: #3d1116;
    --yellow: #d29922;
    --yellow-bg: #2d2000;
    --blue: #58a6ff;
    --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 24px;
    line-height: 1.5;
  }
  h1 {
    font-size: 24px;
    margin-bottom: 4px;
    color: var(--text);
  }
  .subtitle {
    color: var(--text-muted);
    margin-bottom: 20px;
    font-size: 14px;
  }
  .stats {
    display: flex;
    gap: 16px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 20px;
    min-width: 140px;
  }
  .stat-card .num {
    font-size: 28px;
    font-weight: 700;
    color: var(--blue);
  }
  .stat-card .label {
    font-size: 12px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .filter-bar {
    margin-bottom: 16px;
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
  }
  .filter-bar input, .filter-bar select {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 13px;
  }
  .filter-bar input { width: 260px; }
  .filter-bar label {
    font-size: 12px;
    color: var(--text-muted);
  }
  .table-wrapper {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  table {
    border-collapse: collapse;
    width: 100%;
    font-size: 12px;
    white-space: nowrap;
  }
  th {
    background: var(--surface);
    color: var(--text-muted);
    font-weight: 600;
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.5px;
    padding: 10px 12px;
    border-bottom: 2px solid var(--border);
    cursor: pointer;
    user-select: none;
    position: sticky;
    top: 0;
    z-index: 10;
  }
  th:hover { color: var(--blue); }
  th .sort-arrow { margin-left: 4px; opacity: 0.4; }
  th.sorted .sort-arrow { opacity: 1; color: var(--blue); }
  td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  td.wrap {
    white-space: pre-line;
    min-width: 160px;
    max-width: 320px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 11px;
    line-height: 1.6;
  }
  td.secrets-cell {
    white-space: pre-line;
    min-width: 140px;
    font-size: 11px;
  }
  tr:hover td { background: rgba(88,166,255,0.04); }
  td.good {
    background: var(--green-bg);
    color: var(--green);
  }
  td.missing {
    background: var(--red-bg);
    color: var(--red);
  }
  td.warn {
    background: var(--yellow-bg);
    color: var(--yellow);
  }
  td.neutral { color: var(--text-muted); }
  .tag {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 10px;
    font-size: 10px;
    margin: 1px 2px;
    border: 1px solid var(--border);
    color: var(--text-muted);
  }
  .tag-active { border-color: var(--green); color: var(--green); }
  .badge-fork { border-color: var(--purple); color: var(--purple); }
  .badge-sync { border-color: var(--blue); color: var(--blue); }
  a { color: var(--blue); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .tooltip {
    position: relative;
    cursor: help;
  }
  .tooltip:hover::after {
    content: attr(data-tip);
    position: absolute;
    bottom: 100%;
    left: 0;
    background: #1c2128;
    border: 1px solid var(--border);
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 11px;
    white-space: pre-wrap;
    max-width: 400px;
    z-index: 100;
    color: var(--text);
  }
  .legend {
    margin-top: 20px;
    padding: 12px 16px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .legend span { margin-right: 16px; }
</style>
</head>
<body>
<h1>Enablement Framework -- Master Technical Audit</h1>
<p class="subtitle">Generated: """ + datetime.now().strftime("%Y-%m-%d %H:%M") + f""" | Repos: {len(all_rows)} | Org: dynatrace-wwse</p>
""")

# Stats
total = len(all_rows)
with_dc = sum(1 for r in all_rows if r["has_devcontainer"])
with_func = sum(1 for r in all_rows if r["my_function_defs"])
with_badges = sum(1 for r in all_rows if r["readme_badges"] > 0)
sync_managed = sum(1 for r in all_rows if r["sync_managed"] is True)
forks = sum(1 for r in all_rows if r["fork"])
with_rum = sum(1 for r in all_rows if r["has_rum"])
unique_rum = sum(1 for r in all_rows if r.get("rum_unique") == "Unique")
with_mkdocs = sum(1 for r in all_rows if r["has_mkdocs"])
# Phase 1 stats
total_docs = sum(r["doc_count"] for r in all_rows)
total_screenshots = sum(r["screenshot_count"] for r in all_rows)
red_repos = sum(1 for r in all_rows if r["doc_risk"] == "RED")
yellow_repos = sum(1 for r in all_rows if r["doc_risk"] == "YELLOW")
green_repos = sum(1 for r in all_rows if r["doc_risk"] == "GREEN")

html_parts.append(f"""
<div class="stats">
  <div class="stat-card"><div class="num">{total}</div><div class="label">Total Repos</div></div>
  <div class="stat-card"><div class="num">{with_dc}</div><div class="label">Have devcontainer</div></div>
  <div class="stat-card"><div class="num">{sync_managed}</div><div class="label">Sync Managed</div></div>
  <div class="stat-card"><div class="num">{with_mkdocs}</div><div class="label">Have mkdocs.yaml</div></div>
  <div class="stat-card"><div class="num">{with_rum}</div><div class="label">Have RUM Snippet</div></div>
  <div class="stat-card"><div class="num">{unique_rum}</div><div class="label">Unique RUM IDs</div></div>
  <div class="stat-card"><div class="num">{with_badges}</div><div class="label">Have Badges</div></div>
  <div class="stat-card"><div class="num">{forks}</div><div class="label">Forks</div></div>
  <div class="stat-card"><div class="num">{total_docs}</div><div class="label">Total Doc Pages</div></div>
  <div class="stat-card"><div class="num">{total_screenshots}</div><div class="label">Total Screenshots</div></div>
  <div class="stat-card" style="border-color:var(--red);"><div class="num" style="color:var(--red);">{red_repos}</div><div class="label">RED (Gen2 heavy)</div></div>
  <div class="stat-card" style="border-color:var(--yellow);"><div class="num" style="color:var(--yellow);">{yellow_repos}</div><div class="label">YELLOW (Mixed)</div></div>
  <div class="stat-card" style="border-color:var(--green);"><div class="num" style="color:var(--green);">{green_repos}</div><div class="label">GREEN (Gen3)</div></div>
</div>

<div class="filter-bar">
  <label>Filter:</label>
  <input type="text" id="filterInput" placeholder="Search repo name, tag, image..." oninput="filterTable()">
  <label>Status:</label>
  <select id="statusFilter" onchange="filterTable()">
    <option value="">All</option>
    <option value="active">Active</option>
  </select>
  <label>Sync:</label>
  <select id="syncFilter" onchange="filterTable()">
    <option value="">All</option>
    <option value="true">Managed</option>
    <option value="false">Unmanaged</option>
  </select>
  <label>Doc Risk:</label>
  <select id="riskFilter" onchange="filterTable()">
    <option value="">All</option>
    <option value="RED">RED</option>
    <option value="YELLOW">YELLOW</option>
    <option value="GREEN">GREEN</option>
    <option value="N/A">N/A (no docs)</option>
  </select>
</div>

<div class="table-wrapper">
<table id="auditTable">
<thead><tr>
  <th onclick="sortTable(0)">Repo Name <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(1)">Created <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(2)">Fork? <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(3)">Contribs <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(4)">Docker Image <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(5)">Exts # <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(6)">Secrets <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(7)">post-create.sh <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(8)">post-start.sh <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(9)">my_functions.sh <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(10)">Other Envs <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(11)">Badges # <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(12)">README? <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(13)">remoteEnv Keys <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(14)">Ports? <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(15)">mkdocs? <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(16)">RUM Snippet? <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(17)">RUM Unique? <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(18)">Inherits Base? <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(19)">Docs # <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(20)">Screenshots # <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(21)">Gen2 Hits <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(22)">Gen3 Hits <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(23)">Doc Risk <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(24)">Gen2 Keywords <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(25)">Flagged Files <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(26)">Sync <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(27)">Status <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(28)">Image Tier <span class="sort-arrow">&#x25B4;</span></th>
  <th onclick="sortTable(29)">Tags <span class="sort-arrow">&#x25B4;</span></th>
</tr></thead>
<tbody>
""")

for row in all_rows:
    name = row["name"]
    link = f"https://github.com/dynatrace-wwse/{name}"

    # Repo name cell
    html_parts.append(f'<tr data-search="{escape(name)} {escape(" ".join(row["tags"]))} {escape(row["docker_image"])}" data-status="{row["status"]}" data-sync="{str(row["sync_managed"]).lower()}" data-risk="{row["doc_risk"]}">')
    html_parts.append(f'<td><a href="{link}" target="_blank">{escape(name)}</a></td>')

    # Created
    html_parts.append(f'<td class="neutral">{row["created"]}</td>')

    # Fork
    if row["fork"]:
        html_parts.append('<td class="warn"><span class="tag badge-fork">Fork</span></td>')
    else:
        html_parts.append('<td class="neutral">No</td>')

    # Contributors
    html_parts.append(f'<td>{row["contributors"]}</td>')

    # Docker Image
    img = row["docker_image"]
    if img == "N/A":
        html_parts.append('<td class="missing">N/A</td>')
    else:
        short_img = img if len(img) <= 50 else "..." + img[-47:]
        html_parts.append(f'<td class="tooltip" data-tip="{escape(img)}">{escape(short_img)}</td>')

    # Extensions count
    ext_count = row["extensions_count"]
    ext_names = ", ".join(row["extensions"]) if row["extensions"] else "None"
    cls = "good" if ext_count > 0 else "missing"
    html_parts.append(f'<td class="{cls} tooltip" data-tip="{escape(ext_names)}">{ext_count}</td>')

    # Secrets (full list, each on its own line)
    secrets = row["secrets"]
    if secrets:
        secrets_html = "<br>".join(escape(s) for s in secrets)
        html_parts.append(f'<td class="secrets-cell warn">{secrets_html}</td>')
    else:
        html_parts.append('<td class="neutral">None</td>')

    # post-create.sh function calls (each on new line)
    pc_calls = row["post_create_calls"]
    if pc_calls:
        pc_html = "\n".join(escape(f) for f in pc_calls)
        html_parts.append(f'<td class="wrap">{pc_html}</td>')
    else:
        html_parts.append('<td class="missing">N/A</td>')

    # post-start.sh function calls (each on new line)
    ps_calls = row["post_start_calls"]
    if ps_calls:
        ps_html = "\n".join(escape(f) for f in ps_calls)
        html_parts.append(f'<td class="wrap">{ps_html}</td>')
    else:
        html_parts.append('<td class="neutral">—</td>')

    # my_functions.sh definitions (each on new line)
    mf_defs = row["my_function_defs"]
    if mf_defs:
        mf_html = "\n".join(escape(f) + "()" for f in mf_defs)
        html_parts.append(f'<td class="wrap good">{mf_html}</td>')
    else:
        html_parts.append('<td class="missing">N/A</td>')

    # Other envs (env vars from shell files, each on new line)
    other_envs = row["other_envs"]
    if other_envs:
        env_html = "\n".join(escape(e) for e in other_envs)
        html_parts.append(f'<td class="wrap">{env_html}</td>')
    else:
        html_parts.append('<td class="neutral">—</td>')

    # Badges count
    bc = row["readme_badges"]
    cls = "good" if bc > 0 else "missing"
    html_parts.append(f'<td class="{cls}">{bc}</td>')

    # README standard
    if row["has_readme"]:
        if row["readme_standard"]:
            html_parts.append('<td class="good">Standard</td>')
        else:
            html_parts.append('<td class="warn">No badges</td>')
    else:
        html_parts.append('<td class="missing">Missing</td>')

    # remoteEnv keys
    re_keys = row["remote_env_keys"]
    if re_keys:
        html_parts.append(f'<td class="tooltip" data-tip="{escape(", ".join(re_keys))}">{len(re_keys)} keys</td>')
    else:
        html_parts.append('<td class="missing">None</td>')

    # Ports
    if row["has_ports"]:
        html_parts.append(f'<td class="good tooltip" data-tip="{escape(row["ports_summary"])}">Yes</td>')
    else:
        html_parts.append('<td class="missing">No</td>')

    # mkdocs?
    html_parts.append(bool_cell(row["has_mkdocs"]))

    # RUM Snippet
    if row["has_rum"]:
        rum_short = row["rum_hash"][:12] + "..." if len(row["rum_hash"]) > 12 else row["rum_hash"]
        html_parts.append(f'<td class="good tooltip" data-tip="{escape(row["rum_snippet"])}">{escape(rum_short)}</td>')
    else:
        html_parts.append('<td class="missing">Missing</td>')

    # RUM Unique
    ru = row.get("rum_unique", "N/A")
    cls = "neutral"
    if ru == "Unique":
        cls = "good"
    elif ru.startswith("Shared"):
        cls = "warn"
        shared_with = row.get("rum_shared_with", [])
        tip = "Shared with: " + ", ".join(shared_with)
        html_parts.append(f'<td class="{cls} tooltip" data-tip="{escape(tip)}">{ru}</td>')
    else:
        cls = "missing"
    if not ru.startswith("Shared"):
        html_parts.append(f'<td class="{cls}">{ru}</td>')

    # Inherits mkdocs-base?
    html_parts.append(bool_cell(row["inherits_base"]))

    # Phase 1: Doc validation columns
    # Docs count
    dc_count = row["doc_count"]
    if dc_count > 0:
        html_parts.append(f'<td>{dc_count}</td>')
    else:
        html_parts.append('<td class="neutral">—</td>')

    # Screenshots count
    sc_count = row["screenshot_count"]
    if sc_count > 0:
        html_parts.append(f'<td>{sc_count}</td>')
    else:
        html_parts.append('<td class="neutral">—</td>')

    # Gen2 hits
    g2 = row["gen2_count"]
    if g2 > 5:
        html_parts.append(f'<td class="missing">{g2}</td>')
    elif g2 > 0:
        html_parts.append(f'<td class="warn">{g2}</td>')
    else:
        html_parts.append('<td class="good">0</td>')

    # Gen3 hits
    g3 = row["gen3_count"]
    if g3 > 0:
        html_parts.append(f'<td class="good">{g3}</td>')
    else:
        html_parts.append('<td class="neutral">0</td>')

    # Doc Risk badge
    risk = row["doc_risk"]
    if risk == "RED":
        html_parts.append('<td class="missing"><strong>RED</strong></td>')
    elif risk == "YELLOW":
        html_parts.append('<td class="warn"><strong>YELLOW</strong></td>')
    elif risk == "GREEN":
        html_parts.append('<td class="good"><strong>GREEN</strong></td>')
    else:
        html_parts.append('<td class="neutral">N/A</td>')

    # Gen2 keywords found
    g2kw = row["gen2_keywords"]
    if g2kw:
        kw_html = "\n".join(escape(k) for k in g2kw)
        html_parts.append(f'<td class="wrap">{kw_html}</td>')
    else:
        html_parts.append('<td class="neutral">—</td>')

    # Flagged files (red + yellow)
    flagged = row["red_files"] + row["yellow_files"]
    if flagged:
        # Show just filenames, not full paths
        flagged_short = [f.split("/")[-1] if "/" in f else f for f in flagged]
        ff_html = "\n".join(escape(f) for f in flagged_short)
        html_parts.append(f'<td class="wrap">{ff_html}</td>')
    else:
        html_parts.append('<td class="neutral">—</td>')

    # Sync managed
    sm = row["sync_managed"]
    if sm is True:
        html_parts.append('<td class="good"><span class="tag badge-sync">Managed</span></td>')
    else:
        html_parts.append('<td class="warn">Unmanaged</td>')

    # Status
    st = row["status"]
    cls = "good" if st == "active" else "warn"
    html_parts.append(f'<td class="{cls}">{st}</td>')

    # Image tier
    it = row["image_tier"]
    html_parts.append(f'<td>{escape(str(it))}</td>')

    # Tags
    tags_html = " ".join(f'<span class="tag">{escape(t)}</span>' for t in row["tags"])
    html_parts.append(f'<td>{tags_html}</td>')

    html_parts.append('</tr>')

html_parts.append("""
</tbody>
</table>
</div>

<div class="legend">
  <span style="color:var(--green);">&#9632; Green = present/good</span>
  <span style="color:var(--red);">&#9632; Red = missing/needs attention</span>
  <span style="color:var(--yellow);">&#9632; Yellow = warning/shared</span>
  <span style="color:var(--text-muted);">&#9632; Gray = neutral/N/A</span>
  | Hover cells for full details. Click column headers to sort.
</div>

<script>
let sortCol = -1, sortAsc = true;

function sortTable(col) {
  const table = document.getElementById('auditTable');
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.rows);
  const headers = table.tHead.rows[0].cells;

  // Toggle direction
  if (sortCol === col) {
    sortAsc = !sortAsc;
  } else {
    sortCol = col;
    sortAsc = true;
  }

  // Update header styles
  for (let h of headers) h.classList.remove('sorted');
  headers[col].classList.add('sorted');
  headers[col].querySelector('.sort-arrow').textContent = sortAsc ? '\\u25B4' : '\\u25BE';

  rows.sort((a, b) => {
    let av = a.cells[col].textContent.trim();
    let bv = b.cells[col].textContent.trim();
    // Try numeric
    let an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) {
      return sortAsc ? an - bn : bn - an;
    }
    return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  });

  for (let r of rows) tbody.appendChild(r);
}

function filterTable() {
  const filter = document.getElementById('filterInput').value.toLowerCase();
  const statusFilter = document.getElementById('statusFilter').value;
  const syncFilter = document.getElementById('syncFilter').value;
  const riskFilter = document.getElementById('riskFilter').value;
  const rows = document.querySelectorAll('#auditTable tbody tr');

  rows.forEach(row => {
    const search = row.getAttribute('data-search').toLowerCase();
    const status = row.getAttribute('data-status');
    const sync = row.getAttribute('data-sync');
    const risk = row.getAttribute('data-risk');

    let show = true;
    if (filter && !search.includes(filter)) show = false;
    if (statusFilter && status !== statusFilter) show = false;
    if (syncFilter && sync !== syncFilter) show = false;
    if (riskFilter && risk !== riskFilter) show = false;

    row.style.display = show ? '' : 'none';
  });
}
</script>
</body>
</html>
""")

with open(OUTPUT, "w") as f:
    f.write("\n".join(html_parts))

print(f"Generated: {OUTPUT}")
print(f"Total repos: {total}")
print(f"With devcontainer: {with_dc}")
print(f"With custom functions: {with_func}")
print(f"Sync managed: {sync_managed}")
