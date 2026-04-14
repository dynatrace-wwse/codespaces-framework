#!/bin/bash
# Fetch all documentation files from every repo in repos.yaml
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
REPOS_YAML="$SCRIPT_DIR/../repos.yaml"

# Get repo names from repos.yaml
repos=$(python3 -c "import yaml; y=yaml.safe_load(open('$REPOS_YAML')); [print(r['name']) for r in y['repos']]")

for repo in $repos; do
  echo "=== $repo ==="
  docs_dir="$DATA_DIR/$repo/docs"
  mkdir -p "$docs_dir"

  # Get file tree
  tree_json=$(gh api "repos/dynatrace-wwse/$repo/git/trees/main?recursive=1" 2>/dev/null || echo '{"tree":[]}')

  # Extract docs/*.md files
  doc_files=$(echo "$tree_json" | python3 -c "
import sys, json
tree = json.load(sys.stdin).get('tree', [])
for f in tree:
    p = f['path']
    if p.startswith('docs/') and p.endswith('.md'):
        print(p)
" 2>/dev/null)

  # Count images
  img_count=$(echo "$tree_json" | python3 -c "
import sys, json
tree = json.load(sys.stdin).get('tree', [])
count = sum(1 for f in tree if f['path'].startswith('docs/img/') or f['path'].startswith('docs/assets/'))
print(count)
" 2>/dev/null)
  echo "$img_count" > "$DATA_DIR/$repo/img_count.txt"

  if [ -z "$doc_files" ]; then
    echo "  No docs/ directory"
    echo "0" > "$DATA_DIR/$repo/doc_count.txt"
    continue
  fi

  doc_count=0
  for doc_path in $doc_files; do
    # Fetch file content
    content=$(gh api "repos/dynatrace-wwse/$repo/contents/$doc_path" 2>/dev/null | \
      python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['content']).decode())" 2>/dev/null || echo "")

    if [ -n "$content" ]; then
      # Create subdirectories if needed
      doc_subdir=$(dirname "$doc_path" | sed 's|^docs/||; s|^docs$||')
      if [ -n "$doc_subdir" ] && [ "$doc_subdir" != "." ]; then
        mkdir -p "$docs_dir/$doc_subdir"
      fi
      # Save with just filename (strip docs/ prefix)
      save_path="$docs_dir/$(echo "$doc_path" | sed 's|^docs/||')"
      echo "$content" > "$save_path"
      doc_count=$((doc_count + 1))
    fi
  done

  echo "$doc_count" > "$DATA_DIR/$repo/doc_count.txt"
  echo "  Fetched $doc_count docs, $img_count images"
done

echo "Done fetching all documentation."
