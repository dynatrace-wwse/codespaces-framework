# Orbital — moved

Orbital (formerly `ops-server/` in this repository) now lives in the **private**
repository **`dynatrace-wwse/orbital`**, as of 2026-08-27.

It was split out because trainings run customer and prospect workloads: publishing the
control plane's source makes its weak points easier to find. Its operations
documentation — worker paths, setup commands, deploy procedures — moved with it, to
`docs/ops-platform.md` in that repository, for the same reason.

## What stayed here

This repository keeps the devcontainer framework, `sync/`, `repos.yaml`, `audit/` and
these docs. Orbital *reads* them: it consumes `repos.yaml`, and two of its systemd units
(`ops-gen2scan`, `ops-sync-daemon`) run code from a sibling clone of this repository on
every fleet host.

## Layout on a fleet host

```
/home/ops/orbital/                                    ← Orbital (private)
/home/ops/enablement-framework/codespaces-framework/   ← this repo, read-only consumer
```

## History

The last commit of this repository containing `ops-server/` is tagged
**`ops-server-final`**. This repository is public and its history still contains that
code — the split protects future development, not what was already published.
