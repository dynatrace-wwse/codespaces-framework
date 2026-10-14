# dtctl & Monaco in every environment

Every environment the framework builds — GitHub Codespaces, Orbital, a local container — comes up
with two Dynatrace command-line tools already on the `PATH`:

| Tool | What it is for | Pinned version |
|---|---|---|
| [`dtctl`](https://github.com/dynatrace-oss/dtctl) | Query and inspect the tenant: DQL, documents, settings | `DTCTL_VERSION` (0.43.0) |
| [`monaco`](https://github.com/Dynatrace/dynatrace-configuration-as-code) | Configuration as code: deploy dashboards, notebooks, settings | `MONACO_VERSION` (2.30.1) |

No repo has to change anything. `finalizePostCreation` — the step every repo's `post-create.sh`
already calls ("do not remove") — runs `installDevTools` first.

## What `installDevTools` does

1. Reads the switch `FRAMEWORK_DEVTOOLS`. `off` (also `false`, `no`, `0`, any case) skips
   everything below: no download, nothing installed.
2. Installs **dtctl** at `DTCTL_VERSION`, verified against the release's `checksums.txt`.
3. Installs **Monaco** at `MONACO_VERSION`, verified against the release's `.sha256` file.
4. When **`DT_DTCTL_TOKEN`** is set (and `DT_ENVIRONMENT` is), it also runs `enableDtctl`: a
   `readonly` context named `enablement` whose token is the literal reference
   `${DT_DTCTL_TOKEN}`. The token value is never printed and never written to the config.
   Without the token, dtctl is installed with no context.
5. Monaco needs no context: `deployMonaco` reads `DT_MONACO_TOKEN` by name when a repo deploys.

A binary that already reports the pinned version (`<tool> version`) is **kept, not downloaded**.
An image that bakes both tools in therefore pays nothing here — the step only verifies the
version present. A binary at any other version is replaced by the verified pin.

## Never fatal

A failed download or a checksum mismatch **refuses that install** — a binary that does not
match its published checksum is never put on the `PATH` — but it does not stop post-create.
The tool's output is replayed with the tag `devtools (non-fatal)`, followed by one warning:

```
devtools (non-fatal): ... Checksum MISMATCH for dtctl_0.43.0_linux_amd64.tar.gz ...
WARN | devtools (non-fatal): dtctl not installed — post-create continues without it
```

`verifyContainerCreation` filters that tag, so a missing dev tool does not mark the whole
container as failed in the greeting or the tracker. The other tool still installs.

## Turning it off

From the environment (for example a Codespaces secret, or `containerEnv` in
`devcontainer.json`), or in the repo's `.devcontainer/.env`, which `variables.sh` sources:

```bash
FRAMEWORK_DEVTOOLS=off
```

The functions stay available after that — a repo can still call `installMonaco` or
`enableDtctl` on its own.

## Versions

One version of each tool is pinned per framework release in
`.devcontainer/util/variables.sh`, so every environment checksums the same binary instead of
whatever "latest" was on the day:

```bash
DTCTL_VERSION="${DTCTL_VERSION:-0.43.0}"
MONACO_VERSION="${MONACO_VERSION:-2.30.1}"
```

A repo may override either (environment or `.devcontainer/.env`). A value that is not `x.y.z`
is refused before any download. Bumping a pin is a framework release.

## Cost

Measured on an arm64 EC2 host (real release assets, no tenant):

| | Time | Downloaded |
|---|---|---|
| First run (both tools installed) | 1.7 s | 26.5 MB arm64 (dtctl 10.4 MB archive + Monaco 16.1 MB); 29.3 MB on amd64 |
| Re-run, versions already present | 0.04 s | 0 |

On disk: 44 MB (dtctl 28.0 MB + Monaco 16.1 MB). A Codespace's network is slower than an EC2
host's, so expect a few seconds there.

## Related

- [Functions reference — dtctl & Monaco](functions.md#dtctl-monaco): `enableDtctl`,
  `installDtctl`, `installMonaco`, `deployMonaco`, `undeployMonaco`, `verifySha256`, the dtctl
  token caveats and the `monaco/` folder layout.
