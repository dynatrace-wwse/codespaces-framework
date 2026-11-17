# Accepted Critical/High keys — why each is in a baseline

A baseline (`baseline-<tag>.json`) holds keys only; this file holds the reason. Every key added to
a baseline by hand-reviewed refresh gets a row here in the same PR. A key with no row is not
accepted, it is unreviewed.

Published tags are immutable: `publish-image.yaml` refuses to rebuild an existing tag. So a finding
in a published image is fixed by a **new** tag, and the old tag's key is accepted until nothing
pulls it.

## Refresh 2026-11-16 (brief 244, SEC-001) — grype 0.121.0, DB 2026-11-15, run 43799502808

| tag | key | sev | fixed in | why accepted |
|---|---|---|---|---|
| v1.4.0 | CVE-2026-109518\|apt\|2.8.3 | High | v1.4.1 (apt 2.8.4ubuntu0.1) | published tag; 29 training repos + Orbital still pin v1.4.0 until the v1.4.1 rollout (orch/owner decision, open) |
| v1.4.0 | CVE-2026-109518\|apt-utils\|2.8.3 | High | v1.4.1 | same |
| v1.4.0 | CVE-2026-109518\|apt-transport-https\|2.8.3 | High | v1.4.1 | same |
| v1.4.0 | CVE-2026-109518\|libapt-pkg6.0t64\|2.8.3 | High | v1.4.1 | same |
| v1.4.0 | GHSA-2m5v-pch6-74hx\|undici\|6.29.0 | High | v1.4.1 (npm 11.21.2 → undici 6.29.1) | same; undici is npm's bundled copy, reached only when npm runs |
| v1.4.0 | GHSA-qrxm-fqhp-jwvm\|undici\|6.29.0 | High | v1.4.1 | same |
| v1.3.2 | CVE-2026-109518\|apt\|2.8.3, apt-utils, apt-transport-https, libapt-pkg6.0t64 | High | v1.4.1 | superseded tag; one repo (enablement-openinference-aiobs) still pins it, its move is not approved yet |
| v1.3.2 | GHSA-2m5v-pch6-74hx, GHSA-qrxm-fqhp-jwvm\|undici\|6.29.0 | High | v1.4.1 | same |
| v1.3.2 | CVE-2026-111497\|libbrotli1\|1.1.0-2build2 | High | v1.4.0 (apt upgrade, 1.1.0-2ubuntu0.1) | same |
| v1.3.2 | GHSA-mgcc-cww5-fp2q\|go-jose/v4\|v4.1.2 | High | v1.4.0 (containerd removed, #205) | same; in Ubuntu docker.io binaries nothing runs (host socket is mounted) |
| v1.3.2 | GO-2026-6860\|golang.org/x/net\|v0.47.0 | High | v1.4.0 (dockerd/ctr removed, #205) | same |
| v1.3.2 | GO-2026-6865\|klauspost/compress\|v1.18.1, v1.18.2 | High | v1.4.0 (docker.io removed, #205) | same |

v1.4.1's own baseline was written from a fresh scan at publish (45 Critical/High, none new vs v1.4.0).

## 2026-11-17 (brief 249, SEC-001) — v1.3.2 no longer scanned

`enablement-openinference-aiobs`, the last repo pinning v1.3.2, moved to v1.4.1
(enablement-openinference-aiobs #5, `3099e82`; its integration test passed on v1.4.1). Nothing pulls
v1.3.2 any more, so it left the scan matrix and `baseline-v1.3.2.json` was deleted (it stays in git
history). The v1.3.2 rows above are kept as the record of what was accepted while it was pulled.
