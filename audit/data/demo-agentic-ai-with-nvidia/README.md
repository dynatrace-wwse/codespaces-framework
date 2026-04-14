<!-- markdownlint-disable-next-line -->
<!-- Notes to Self
[x] migrate repo
[x] check that works
[x] clean and move app to folder, clean architecture
[x] clean and move images and docs to folder
[x] add RUM 
[x] move docs to gh pages
[X] Rename repo to demo-agentic-ai-with-nvidia
[X] add integration tests: basic tests added. 
[x] trim to 2 cores

[ ] Adapt greeting to no Kubernetes Cluster, same as Azure WebApp Repo.


Documentation
[ ] have better guidance, like this  https://dynatrace-wwse.github.io/enablement-gen-ai-llm-observability/ with explanations, should be self-service. bring more customers and prospects! we should wow them.
[ ] Documentation on getting the token,
  - LLM token better like this: https://dynatrace-wwse.github.io/enablement-gen-ai-llm-observability/2-getting-started/

 Refactor Vars to 
  [ ] "DT_ENVIRONMENT": {"description": "URL to your Dynatrace Platform eg. https://abc123.apps.dynatrace.com or for sprint -> https://abc123.sprint.apps.dynatracelabs.com"
  [ ] "DT_LLM_TOKEN": {"description": "Dynatrace API token with these permissions: Ingest events, Ingest logs, Ingest metrics, Ingest OpenTelemetry traces"
  [ ] clear writing to bashrc and move -> .zshrc or .env

Improve Dev Experience
[x] Start app in backgrpund and manage process with PID (release terminal)
[x] Stop app with PID
[x] Init app
[x] Pipe log std errand std output to logfile
[x] function to read App log with "less +F" 
[x] function to read Otel log with "less +F"

[ ] Protect branch with git strategy
[ ] Version policy

-->

# <img src="https://cdn.bfldr.com/B686QPH3/at/w5hnjzb32k5wcrcxnwcx4ckg/Dynatrace_signet_RGB_HTML.svg?auto=webp&format=pngg" alt="DT logo" width="30"> NVIDIA Guardrails and Dynatrace Insights
[![Dynatrace](https://img.shields.io/badge/Dynatrace-Intelligence-purple?logo=dynatrace&logoColor=white)](https://dynatrace-wwse.github.io/codespaces-framework/dynatrace-integration/#mcp-server-integration)
[![Mastering](https://img.shields.io/badge/Mastering-Complexity-8A2BE2?logo=dynatrace)](https://dynatrace-wwse.github.io)
[![Downloads](https://img.shields.io/docker/pulls/shinojosa/dt-enablement?logo=docker)](https://hub.docker.com/r/shinojosa/dt-enablement)
[![Integration tests](https://github.com/dynatrace-wwse/demo-agentic-ai-with-nvidia/actions/workflows/integration-tests.yaml/badge.svg)](https://github.com/dynatrace-wwse/demo-agentic-ai-with-nvidia/actions)
[![Version](https://img.shields.io/github/v/release/dynatrace-wwse/demo-agentic-ai-with-nvidia?color=blueviolet)](https://github.com/dynatrace-wwse/demo-agentic-ai-with-nvidia/releases)
[![Commits](https://img.shields.io/github/commits-since/dynatrace-wwse/demo-agentic-ai-with-nvidia/latest?color=ff69b4&include_prereleases)](https://github.com/dynatrace-wwse/demo-agentic-ai-with-nvidia/graphs/commit-activity)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/dynatrace-wwse/demo-agentic-ai-with-nvidia/blob/main/LICENSE)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-green)](https://dynatrace-wwse.github.io/demo-agentic-ai-with-nvidia/)

___

<p align="center">
  <img src="docs/img/nvidia-workshop-header.png">
</p>

## 🚀 Demo Overview

This repository is a **demo project** showcasing the **NVIDIA NeMo Agent Toolkit** with **Dynatrace integration**.

<p align="center">
  <img src="docs/img/dt-nvidia.png" width="50%">
</p>


It is intended for hands-on exploration of how agent-based AI workflows can be built, run, and observed in a practical setup. With this demo, you can:

- run and inspect an agent-based AI implementation using NVIDIA NeMo Agent Toolkit  
- observe behavior, telemetry, and AI monitoring signals with Dynatrace  
- use a structured example that can be adapted for workshops, demos, or technical walkthroughs  

Use this README to understand the demo scope, architecture, and expected outcomes.




## 📚 Documentation

For setup, usage, and implementation details, visit the [GitHub Pages documentation](https://dynatrace-wwse.github.io/demo-agentic-ai-with-nvidia)