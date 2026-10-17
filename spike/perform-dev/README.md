# Spike 92 — Perform developer sessions (GitLab on K3d + browser IDE)

Throwaway spike code. **Not product, do not merge.** Report: `~/worktrees/orch-main/92-REPORT.md`.

| file | what |
|---|---|
| `gitlab-values.yaml` | GitLab chart 9.4.0 trimmed (no registry/KAS/Prometheus/shell/toolbox, 1 puma worker), external URL = Orbital subdomain, MinIO from GitLab's mirror (Docker Hub tags are gone) |
| `replica_subdomain_proxy_fixed.py` | Orbital `proxy_subdomain_app` copied verbatim + the 3 `SPIKE FIX` edits that made GitLab login/git work |
| `replica-nginx-subdomain.conf` | replica of the `ops-server.conf` subdomain block + `client_max_body_size` (SPIKE FIX 4) |
| `gl-login.sh` | form login with CSRF; password read into a variable, never printed |
| `git-via.sh` | clone + N MB push over HTTPS through the replica; PAT via GIT_ASKPASS |
| `ide-probe.mjs` | headless Chromium: load code-server, edit+save a file, run `kubectl get pods -A` in the integrated terminal, record every WebSocket |
| `cs-restart.sh` | restart code-server in `dt` and measure idle RSS |
| `omnibus-run.sh` | Omnibus CE with GitLab's memory-constrained settings, for comparison |
