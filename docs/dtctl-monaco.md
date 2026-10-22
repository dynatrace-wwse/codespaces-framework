# dtctl & Monaco in every environment

Every environment the framework builds — GitHub Codespaces, Orbital, a local container — comes up
with two Dynatrace command-line tools already on the `PATH`:

| Tool | What it is for | Pinned version |
|---|---|---|
| [`dtctl`](https://github.com/dynatrace-oss/dtctl) | Query and inspect the tenant: DQL, documents, settings | `DTCTL_VERSION` (0.43.0) |
| [`monaco`](https://github.com/Dynatrace/dynatrace-configuration-as-code) | Configuration as code: deploy dashboards, notebooks, settings | `MONACO_VERSION` (2.30.1, the framework's patched build `2.30.1+wwse.1` — see [below](#monaco-is-a-patched-build)) |

No repo has to change anything. `finalizePostCreation` — the step every repo's `post-create.sh`
already calls ("do not remove") — runs `installDevTools` first.

## What `installDevTools` does

1. Reads the switch `FRAMEWORK_DEVTOOLS`. `off` (also `false`, `no`, `0`, any case) skips
   everything below: no download, nothing installed.
2. Installs **dtctl** at `DTCTL_VERSION`, verified against the **reviewed sha256 committed in
   the framework** first, and against the release's `checksums.txt` second.
3. Installs **Monaco** at `MONACO_VERSION`. For the framework's pinned version that is the
   framework's **patched build** ([why](#monaco-is-a-patched-build)), verified against the
   reviewed sha256 only; another version is the upstream release, verified like dtctl (its
   `.sha256` file second).
4. When **`DT_DTCTL_TOKEN`** is set (and `DT_ENVIRONMENT` is), it also runs `enableDtctl`: a
   `readonly` context named `enablement` whose token is the literal reference
   `${DT_DTCTL_TOKEN}`. The token value is never printed and never written to the config, and
   the config is left **read-only** (see below). Without the token, dtctl is installed with no
   context.
5. Monaco needs no context: `deployMonaco` reads `DT_MONACO_TOKEN` by name when a repo deploys.
   How a repo deploys and undoes it: [The `monaco/` folder](functions.md#the-monaco-folder), or
   `deployMonaco -h` / `undeployMonaco -h` in the terminal.

A binary already present is **kept, not downloaded**, only when its sha256 equals the committed
pin for the installed binary **and** `<tool> version` names the pinned version. An image that
bakes both tools in therefore pays nothing here — the step only verifies what is present. Any
other binary (other bytes, other version) is replaced by the verified pin.

The architecture is always `uname -m`: `x86_64`/`amd64` or `aarch64`/`arm64`. A 32-bit ARM
userland (`armv8l`, `armv7l`) is refused — it cannot run the arm64 binary. A stray `ARCH`
variable cannot select the wrong binary.

## Supply chain

The release's checksum file comes from the same place as the binary, so on its own it proves
only that the download is intact, not that it is the build that was reviewed. The framework
therefore commits, next to each pinned version in `.devcontainer/util/variables.sh`:

| Variable | What it hashes |
|---|---|
| `DTCTL_PIN_SHA256_AMD64` / `_ARM64` | the dtctl release archive (`.tar.gz`) |
| `DTCTL_PIN_BIN_SHA256_AMD64` / `_ARM64` | the dtctl binary inside it (for the already-present check) |
| `MONACO_PIN_SHA256_AMD64` / `_ARM64` | the Monaco binary — for the pinned version, the framework's **patched** build at `MONACO_PIN_URL` |
| `MONACO_PIN_BUILD` / `MONACO_PIN_URL` | the patched build's suffix (`wwse.1`) and the release it is downloaded from |

A download that does not match the committed hash is refused even when the release's checksum
file agrees with it. For the framework's own pinned version a **missing** hash for the machine's
architecture also refuses — there is no fallback to the release file. Only a repo that overrides
`DTCTL_VERSION` / `MONACO_VERSION` to another version gets no reviewed hash; that install warns
and checks the release file only. **Bumping a version is a
framework release that bumps its hashes in the same commit.**

### Monaco is a patched build

Stock Monaco 2.30 calls `GET /platform/metadata/v1/classic-environment-domain` before every
`deploy`, `delete` or `download` with a platform token. That endpoint needs
`app-engine:apps:run`. A learner's token must not carry that scope, because it opens the
Enablement app's own functions to the learner. So with stock Monaco, `deployMonaco` and
`undeployMonaco` do nothing in a learner environment (ENH-114).

The framework therefore installs **Monaco 2.30.1 plus a 2-file patch**: Monaco looks up the
classic URL only when the manifest also has a classic access token, which is the only time it
uses it. `deployMonaco` writes platform-token manifests, so they never make the call.

- **Source:** upstream tag `v2.30.1` (`8eb538d04`) plus
  [`monaco-patched/enh114-skip-discovery.patch`](https://github.com/dynatrace-wwse/codespaces-framework/tree/main/monaco-patched).
- **Build:** `monaco-patched/build.sh`. It uses the same Go (1.26.8) and flags as upstream's
  release, plus `-trimpath`. Two builds produce byte-identical binaries, so anyone can rebuild
  the release and compare the sha256. `monaco version` reports `2.30.1+wwse.1`.
- **Published** as release `monaco-2.30.1-wwse.1` of this repo (`MONACO_PIN_URL`). That adds no
  new trust: whoever can publish a release here can already change `functions.sh`.
- **Install:** the download must match `MONACO_PIN_SHA256_<ARCH>`. A mismatch, a missing pin, a
  non-`https://` pin URL or a failed download refuses the install. **The stock release is never
  installed in its place**, because it would look installed and then fail every deploy — and a
  refusal also **removes** any `monaco` already in the install directory (a stock binary baked
  into an image), so nothing unverified is left there.
- **Use:** `deployMonaco` and `undeployMonaco` check the `monaco` on the `PATH` before running
  it: its sha256 must equal the pinned binary hash, or `monaco version` must name
  `+wwse.1`. Anything else gets one `installMonaco`; still not verified, they fail loudly with
  `MONACO NOT VERIFIED: …` and run nothing (a `monaco` elsewhere on the `PATH` that the install
  cannot replace is refused the same way). `undeployMonaco` then keeps its delete file.
- **CI guards** (`.github/workflows/monaco-pin.yaml`): *release-assets* downloads each pinned
  asset from `MONACO_PIN_URL` and runs `sha256sum -c` against the pins — it fails while the
  release is unpublished, which is what stops the pin being merged first; *reproducible-build*
  rebuilds from `v2.30.1` + the patch with `build.sh` and checks the result against the same
  pins.
- **Upstream:** proposed in
  [Discussion #2208](https://github.com/Dynatrace/dynatrace-configuration-as-code/discussions/2208).
  Once an upstream release has the guard, the pin goes back to that release and its hashes, and
  `MONACO_PIN_BUILD`, `MONACO_PIN_URL` and `monaco-patched/` are removed.

**`undeployMonaco` leaves documents in the trash.** The learner token cannot purge the trash
(no `document:trash.documents:delete`), so Monaco's purge after each document delete is
refused with 403. `undeployMonaco` treats that — and only that — as success with a warning;
see [dtctl & Monaco in functions](functions.md#dtctl-monaco), "Deleted documents stay in the trash".
A trashed document keeps its id, so the next `deployMonaco` uses project `<repo>-<DT_SESSION_ID>-g<N>`
and fresh ids; without that the redeploy answered `409 "id already exists"` ("A trashed document
keeps its id").

A repo that overrides `MONACO_VERSION` gets the **stock** release from `installMonaco`, but
`deployMonaco` / `undeployMonaco` refuse to run it while the framework pins a patched build
(`MONACO_PIN_URL` set): stock Monaco needs `app-engine:apps:run`, which a learner token does
not carry.

## Never fatal

A failed download or a checksum mismatch **refuses that install** — a binary that does not
match its published checksum is never put on the `PATH` — but it does not stop post-create.
The tool's output is replayed with the tag `devtools (non-fatal)`, followed by one warning:

```
devtools (non-fatal): ... monaco: download of the patched v2.30.1+wwse.1 failed ...
WARN | devtools (non-fatal): Monaco not installed — post-create continues without it
```

`verifyContainerCreation` filters that tag (`_creationNoiseFilter`), so a missing dev tool
does not mark the whole container as failed in the greeting or the tracker.

An **integrity** refusal is different: a checksum mismatch, a missing reviewed hash or a
non-`https://` pin URL (every such message starts `INTEGRITY:`) is replayed with the tag
`devtools INTEGRITY FAILED`, which the filter **keeps**. Post-create still carries on, but the
container is reported as having an error — a tampered download is not noise.

```
devtools INTEGRITY FAILED: ... INTEGRITY: Checksum MISMATCH for dtctl_0.43.0_linux_amd64.tar.gz ...
WARN | devtools (non-fatal): dtctl not installed — post-create continues without it
``` The other tool
still installs. Token values are masked (`<redacted>`) in everything replayed.

Nothing in the step can hang post-create:

- every download has `--connect-timeout 10`, and both tools share **one time budget** of
  180 s (`DEVTOOLS_TIME_BUDGET`; anything but a whole number of seconds falls back to 180); once spent, the remaining downloads are skipped as a failure;
- `sudo` runs as `sudo -n`, so a box without passwordless sudo fails at once instead of
  waiting on a password prompt.

## The dtctl config is read-only

dtctl 0.43 rewrites its whole config on **any** `dtctl config` write — `use-context`, or
`set-credentials` for another context — with every `${VAR}` **expanded**, which puts the token
value on disk. Checked with the real 0.43.0 binary and a fake token in a throwaway `HOME`:
`enableDtctl` leaves only `~/.config/dtctl/config`, holding the reference; nothing is written
under `~/.local/share/dtctl`, and read commands do not write.

`enableDtctl` defends twice:

- it runs every one of its own `dtctl config` writes in an **allowlist environment** (`env -i`
  with only `PATH`, `HOME`, `XDG_CONFIG_HOME`, `DTCTL_CONFIG`, `DTCTL_TOKEN_STORAGE`), so no token
  variable is present and an expansion can only ever produce `""`, never a value — not even for
  a moment. The last write puts the reference back;
- it leaves the config `0400`. Read commands work, and a later `dtctl config` write fails with
  `permission denied` instead of storing the token. dtctl 0.43.0 writes the config **in place**
  (`open(O_WRONLY|O_TRUNC)` on the file; no temporary file, no rename — checked with `strace`),
  so the file mode is what stops it. A dtctl version that switched to temp-file-and-rename would
  bypass a file mode: re-check this on every version bump.

Re-run `enableDtctl` to change the context — it restores the mode itself.

**Do not run `dtctl config view`.** It prints the **resolved** token — the value, not the
reference.

!!! note "The framework owns only its own context"
    `enableDtctl` manages the `enablement` context. Its config writes run with **no** token
    variable in dtctl's environment, and dtctl rewrites the whole file on each write, so **every
    other context's `${VAR}` token reference — `${DT_DTCTL_TOKEN}` or any other — is rewritten
    as an empty token** (`token: ""`). Only `enablement` gets its reference back. A value never
    lands on disk; a context you added by hand must be set up again after `enableDtctl`, or kept
    in a separate config file (`dtctl --config <file>`).

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
MONACO_VERSION="${MONACO_VERSION:-2.30.1}"   # installs the patched 2.30.1+wwse.1
```

A repo may override either (environment or `.devcontainer/.env`). A value that is not `x.y.z`
is refused before any download. Bumping a pin is a framework release.

## Cost

Measured on an arm64 EC2 host (real release assets, no tenant):

| | Time | Downloaded |
|---|---|---|
| First run (both tools installed) | 0.7–1.7 s | 26.5 MB arm64 (dtctl 10.4 MB archive + Monaco 16.1 MB); 29.3 MB on amd64 |
| Re-run, versions already present (sha256 + version checked) | 0.08 s | 0 |

On disk: 44 MB (dtctl 28.0 MB + Monaco 16.1 MB). A Codespace's network is slower than an EC2
host's, so expect a few seconds there.

## Related

- [Functions reference — dtctl & Monaco](functions.md#dtctl-monaco): `enableDtctl`,
  `installDtctl`, `installMonaco`, `deployMonaco`, `undeployMonaco`, `verifySha256`, the dtctl
  token caveats and the `monaco/` folder layout.
