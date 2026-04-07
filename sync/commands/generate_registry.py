"""sync generate-registry — Generate HTML registry page."""

import json
import sys
from datetime import datetime, timezone

from sync.core.repos import load_repos
from sync.core.github_api import get_latest_tags, GHAPIError


CARD_TEMPLATE = """    <div class="repo-card" data-status="{status}">
      <div class="card-header">
        <h3>{name}</h3>
        <span class="status-badge status-{status}">{status}</span>
      </div>
      <p class="description">{description}</p>
      <div class="card-meta">
        <span class="version-pill" title="{latest_tag}">{latest_tag_short}</span>
        <span class="maintainer">{maintainer}</span>
      </div>
      <div class="card-actions">
        <a href="https://github.com/{repo}" target="_blank">GitHub</a>
        <a href="https://{org}.github.io/{repo_name}" target="_blank">Docs</a>
        <a href="https://github.com/codespaces/new?repo={repo}" target="_blank">Open in Codespaces</a>
      </div>
    </div>"""


def run(args):
    output_path = args.output
    repos = [r for r in load_repos() if r.status == "active"]

    cards = []
    for repo_entry in repos:
        owner, name = repo_entry.owner, repo_entry.repo_name
        try:
            tags = get_latest_tags(owner, name)
            combined = [t for t in tags if "_" in t]
            latest_tag = combined[0] if combined else "untagged"
        except GHAPIError:
            latest_tag = "unknown"

        tag_short = latest_tag if len(latest_tag) < 20 else latest_tag[:17] + "..."
        cards.append(
            CARD_TEMPLATE.format(
                name=repo_entry.name,
                status=repo_entry.status,
                description=repo_entry.description,
                latest_tag=latest_tag,
                latest_tag_short=tag_short,
                maintainer=repo_entry.maintainer,
                repo=repo_entry.repo,
                org=repo_entry.owner,
                repo_name=repo_entry.repo_name,
            )
        )

    registry_html = "\n".join(cards)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Try to inject into existing file
    try:
        with open(output_path) as f:
            existing = f.read()

        if "<!-- REGISTRY-START -->" in existing and "<!-- REGISTRY-END -->" in existing:
            import re

            updated = re.sub(
                r"<!-- REGISTRY-START -->.*<!-- REGISTRY-END -->",
                f"<!-- REGISTRY-START -->\n{registry_html}\n    <!-- REGISTRY-END -->",
                existing,
                flags=re.DOTALL,
            )
            with open(output_path, "w") as f:
                f.write(updated)
            print(f"+ Updated registry in {output_path} ({len(cards)} repos)")
            print(f"  Last updated: {timestamp}")
            return
    except FileNotFoundError:
        pass

    # Generate standalone page
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dynatrace WWSE Enablement Registry</title>
  <style>
    :root {{ --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --text-2: #94a3b8; --accent: #7c3aed; --green: #4ade80; --red: #fb7185; }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 2rem; }}
    h1 {{ margin-bottom: 1.5rem; }}
    .registry {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 1.5rem; }}
    .repo-card {{ background: var(--card); border-radius: 12px; padding: 1.5rem; }}
    .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; }}
    .card-header h3 {{ font-size: 1.1rem; }}
    .status-badge {{ font-size: 0.75rem; padding: 2px 8px; border-radius: 9999px; text-transform: uppercase; }}
    .status-active {{ background: #065f46; color: #6ee7b7; }}
    .status-experimental {{ background: #78350f; color: #fbbf24; }}
    .status-archived {{ background: #374151; color: #9ca3af; }}
    .description {{ color: var(--text-2); font-size: 0.9rem; margin-bottom: 1rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .card-meta {{ display: flex; gap: 0.75rem; align-items: center; margin-bottom: 1rem; font-size: 0.8rem; }}
    .version-pill {{ background: var(--accent); padding: 2px 8px; border-radius: 9999px; font-family: monospace; }}
    .maintainer {{ color: var(--text-2); }}
    .card-actions {{ display: flex; gap: 0.5rem; }}
    .card-actions a {{ font-size: 0.8rem; padding: 4px 12px; border: 1px solid #334155; border-radius: 6px; color: var(--text); text-decoration: none; }}
    .card-actions a:hover {{ background: #334155; }}
    .timestamp {{ color: var(--text-2); font-size: 0.8rem; margin-top: 2rem; }}
    @media (max-width: 640px) {{ .registry {{ grid-template-columns: 1fr; }} .card-actions {{ flex-direction: column; }} }}
  </style>
</head>
<body>
  <h1>Dynatrace WWSE Enablement Registry</h1>
  <div class="registry">
<!-- REGISTRY-START -->
{registry_html}
    <!-- REGISTRY-END -->
  </div>
  <p class="timestamp">Last updated: {timestamp}</p>
</body>
</html>
"""

    with open(output_path, "w") as f:
        f.write(html)
    print(f"+ Generated registry at {output_path} ({len(cards)} repos)")
    print(f"  Last updated: {timestamp}")
