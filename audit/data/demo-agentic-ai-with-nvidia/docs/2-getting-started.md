--8<-- "snippets/getting-started.js"
--8<-- "snippets/grail-requirements.md"

## Prerequisites

This repo and related guides assume Mac OS/Linux, but you can adapt as required for Windows.

1. NVIDIA Build Account on [build.nvidia.com](https://build.nvidia.com)
1. Tavily Developer Account on [tavily.com](https://www.tavily.com)
1. Dynatrace Tenant. For a Trial, visit [Dynatrace signup page](https://www.dynatrace.com/signup/)
1. [Dynatrace API Token](https://docs.dynatrace.com/docs/dynatrace-api/basics/dynatrace-api-authentication#create-token) with the required scopes for the OTLP Ingest API:
    * `openTelemetryTrace.ingest`
    * `metrics.ingest`
    * `logs.ingest`

If you are not using GitHub codespaces and want to install and run locally, then you will also need:
- Python 3.11, 3.12, or 3.13 
- Python package and project manager, [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker or Podman for containerized deployment of a OpenTelemetry Collector 


<div class="grid cards" markdown>
- [Let's launch Codespaces:octicons-arrow-right-24:](3-codespaces.md)
</div>
