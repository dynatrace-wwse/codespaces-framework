
--8<-- "snippets/versioning.js"

## 📋 Versioning Policy

This framework and its repositories follow a consistent versioning scheme to ensure clarity and compatibility.

### Format

Versions use the format: **`<framework_version>+<repository_version>`**

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
| `1.0.0+1.0.0` | Initial release (framework v1.0.0, repository v1.0.0) |
| `1.1.0+1.0.0` | Framework upgraded to v1.1.0, repository unchanged |
| `1.1.0+1.1.0` | Repository upgraded to v1.1.0, framework remains v1.1.0 |
| `1.1.2+1.1.1` | Framework patch (v1.1.2) and repository patch (v1.1.1) |

### Benefits

- **Clarity**: Immediate visibility of framework and repository versions
- **Compatibility**: Aligns with SemVer and tooling expectations
- **Flexibility**: Independent evolution of framework and repositories


<div class="grid cards" markdown>
- [Continue to Resources →](resources.md)
</div>
