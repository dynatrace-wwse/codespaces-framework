# Validation reports

Generated output from Orbital's agentic validator. Each subdirectory is a
repository slug; each file is a dated run: `{repo}/YYYY-MM-DD.md`.

| Field | Value |
|---|---|
| Writer | `agentic_validator.py` in the private Orbital repository |
| Path | hardcoded — `validation-reports/{repo}/YYYY-MM-DD.md` |
| Trigger | a manual validation run via Orbital's API |
| Latest run committed | 2026-05-25 |

**Nothing here is current.** The 2026-05-25 files are a historical snapshot.
When a newer run completes, the writer adds a new dated file alongside the
existing ones; it does not overwrite them.

Do not edit these files. Do not move this directory — the writer's path is
hardcoded and a move would cause future runs to create a new directory instead
of depositing files here.

The nine reports cover: `demo-agentic-ai-with-nvidia`, `demo-astroshop-problems`,
`enablement-dynatrace-ai-mcp`, `enablement-dynatrace-log-ingest-101`,
`enablement-kubernetes-opentelemetry-openpipeline`, `enablement-live-debugger-bug-hunting`,
`enablement-workflow-essentials`, `workshop-destination-automation`,
`workshop-dynatrace-log-analytics`. These are the repositories the validator had
access to on the day of the run, not necessarily the full current fleet.
