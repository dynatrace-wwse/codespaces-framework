

## 📋 Versioning Policy

This framework and its repositories follow a consistent versioning scheme to ensure clarity and compatibility.

### Format

Versions use the format: **`v<framework_version>_<repository_version>`**

Both components follow [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`):

- **Framework version**: Infrastructure and technical components
  - `MAJOR`: Breaking changes in framework architecture
  - `MINOR`: Backward-compatible feature additions
  - `PATCH`: Bug fixes or minor improvements

- **Repository version**: Tutorial content and structure
  - `MAJOR`: Breaking changes in tutorial content or structure
  - `MINOR`: New tutorials or enhancements
  - `PATCH`: Minor corrections or fixes

### Examples

| Version | Meaning |
|---------|---------|
| `v1.0.0_1.0.0` | Initial release |
| `v1.2.0_1.0.0` | Framework upgraded, repository unchanged |
| `v1.2.0_1.1.0` | Repository upgraded, framework unchanged |
| `v1.2.5_1.0.3` | Framework at 1.2.5, repository at patch 1.0.3 |

### Version Pin

Each repo pins its framework version in `.devcontainer/util/source_framework.sh`:

```bash
FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-<framework-version>}"
```

That one line is the single selector: it decides which framework files the container pulls from the
cache **and** which `mkdocs-base.yaml` the docs workflow fetches. The Sync CLI manages the pin across
all repos via `push-update`.

!!! tip "Read the version from the repo, never from a document"
    Any version written into a page is stale the next release. To see what a repo actually pins:
    ```bash
    grep -oP ':-\K[^}"]+' .devcontainer/util/source_framework.sh | head -1
    ```
    Across the whole fleet, `sync status` shows the drift; the framework's own current version is its
    latest git tag.

### Tagging Workflow

After syncing all repos to a framework version and merging PRs:

```bash
# Create combined tags on all repos
sync tag --framework-version <framework-version>

# Bump repo version and create GitHub Releases
sync tag --framework-version <framework-version> --bump patch --release
```

### Benefits

- **Clarity**: Immediate visibility of framework and repository versions
- **Compatibility**: Aligns with SemVer and tooling expectations
- **Flexibility**: Independent evolution of framework and repositories
- **Automation**: Sync CLI manages version bumps, tags, and releases across all repos


<div class="grid cards" markdown>
- [Continue to Cleanup →](cleanup.md)
</div>
