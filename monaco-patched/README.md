# Patched Monaco (ENH-114)

The framework installs Monaco **2.30.1 plus `enh114-skip-discovery.patch`**, published as the
release [`monaco-2.30.1-wwse.1`](https://github.com/dynatrace-wwse/codespaces-framework/releases/tag/monaco-2.30.1-wwse.1).
Why it exists, and how `installMonaco` verifies it: [docs/dtctl-monaco.md](../docs/dtctl-monaco.md#monaco-is-a-patched-build).

```bash
./build.sh ./out        # needs git + any go >= 1.21; fetches go1.26.8 itself
cat out/*.sha256        # must equal MONACO_PIN_SHA256_* in .devcontainer/util/variables.sh
```

The build is reproducible: two runs, with separate module and build caches, produce
byte-identical binaries.

**Drop this directory** once an upstream release contains the guard
([Discussion #2208](https://github.com/Dynatrace/dynatrace-configuration-as-code/discussions/2208)).
Then point `MONACO_PIN_VERSION` and its hashes back at that upstream release, and remove
`MONACO_PIN_BUILD` and `MONACO_PIN_URL`.
