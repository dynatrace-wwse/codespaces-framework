

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

### Vendored Front-End Assets

Mermaid is **vendored**, not pulled from a CDN at page load. Material for MkDocs would
otherwise fetch `https://unpkg.com/mermaid@11/dist/mermaid.min.js` on every page view — a
floating major that can change under the sites without a commit anywhere.

| | |
|---|---|
| File | `docs/javascripts/mermaid.min.js` |
| Pinned version | `11.17.2` |
| Source | `https://unpkg.com/mermaid@11.17.2/dist/mermaid.min.js` |
| sha256 | `581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8` |
| Size | 3.5 MB raw (0.9 MB gzipped over the wire) |

Consuming repos do not commit the file. Their `deploy-ghpages.yaml` fetches it from this repo
at their pinned `FRAMEWORK_VERSION`, alongside `mkdocs-base.yaml` and `docs/stylesheets/extra.css`.
The pin therefore travels with the framework version — every repo on the same tag renders with
the same mermaid.

!!! warning "The pin does not update itself"
    There are no automatic security or bug fixes. A diagram written against mermaid syntax newer
    than the pin renders as an error box in the page; it does **not** fail the build.

#### Bumping the pin

1. Check what changed upstream: [mermaid releases](https://github.com/mermaid-js/mermaid/releases).
2. Download and record the new hash:
   ```bash
   curl -fsSL "https://unpkg.com/mermaid@<NEW>/dist/mermaid.min.js" -o docs/javascripts/mermaid.min.js
   sha256sum docs/javascripts/mermaid.min.js
   ```
3. Update the version, source URL and sha256 in the `extra_javascript` comment block of
   `mkdocs-base.yaml`, and the table above.
4. Build and confirm a diagram still renders — `mkdocs build` succeeding is **not** proof. Check
   that the page emits `<pre class="mermaid">` and that a diagram appears in the browser. Material
   puts the rendered SVG in a *closed* shadow root, so `document.querySelector(".mermaid svg")`
   finds nothing even when the diagram is fine.
5. Release a framework version and `sync push-update`, so every repo moves to the same mermaid.
   Repos left on older tags keep the mermaid of the tag they are pinned to.

Staying inside mermaid `11.x` is deliberate: Material 9.5.x asks for `mermaid@11`, so the pin
matches what it expects. A major bump needs a Material upgrade checked alongside it.

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
