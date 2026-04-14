<!-- markdownlint-disable-next-line -->
# <img src="https://cdn.bfldr.com/B686QPH3/at/w5hnjzb32k5wcrcxnwcx4ckg/Dynatrace_signet_RGB_HTML.svg?auto=webp&format=pngg" alt="DT logo" width="45"> Dynatrace Enablement Framework

[![Dynatrace](https://img.shields.io/badge/Dynatrace-Intelligence-purple?logo=dynatrace&logoColor=white)](https://dynatrace-wwse.github.io/codespaces-framework/dynatrace-integration/#mcp-server-integration)
[![Mastering](https://img.shields.io/badge/Mastering-Complexity-8A2BE2?logo=dynatrace)](https://dynatrace-wwse.github.io)
[![Downloads](https://img.shields.io/docker/pulls/shinojosa/dt-enablement?logo=docker)](https://hub.docker.com/r/shinojosa/dt-enablement)
[![Integration tests](https://github.com/dynatrace-wwse/codespaces-framework/actions/workflows/integration-tests.yaml/badge.svg)](https://github.com/dynatrace-wwse/codespaces-framework/actions)
[![Version](https://img.shields.io/github/v/release/dynatrace-wwse/codespaces-framework?color=blueviolet)](https://github.com/dynatrace-wwse/codespaces-framework/releases)
[![Commits](https://img.shields.io/github/commits-since/dynatrace-wwse/codespaces-framework/latest?color=ff69b4&include_prereleases)](https://github.com/dynatrace-wwse/codespaces-framework/graphs/commit-activity)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/dynatrace-wwse/codespaces-framework/blob/main/LICENSE)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-green)](https://dynatrace-wwse.github.io/codespaces-framework/)


___


The Dynatrace Enablement Framework streamlines the delivery of demos and hands-on trainings for the Dynatrace Platform. It provides a unified set of tools, templates, and best practices to ensure trainings are easy to create, run anywhere, and maintain over time.



<p align="center">
  <img src="docs/img/framework_banner.png" alt="DT Enablement Framework">
</p>


This repository is the source of truth for the framework: the container image, sync CLI, core shell functions, and templates that power every enablement lab.

___

### What's included

- **Container image** — Pre-built dev environment with Kind, kubectl, Helm, and Dynatrace CLI tools (`shinojosa/dt-enablement`)
- **Sync CLI** — Manages versioned updates, migrations, PRs, tagging, and releases across all consumer repos
- **Versioned pull model** — Each repo pins a `FRAMEWORK_VERSION` and pulls core functions from a cached release at startup
- **MkDocs documentation** — Shared base config with RUM tracking, auto-deployed to GitHub Pages
- **Integration tests** — CI pipeline that validates the framework inside a real Codespace environment

### Enablement registry

Browse all available labs, demos, and workshops with live CI status and documentation links:

**[dynatrace-wwse.github.io](https://dynatrace-wwse.github.io)**

### Documentation

**[📖 Full documentation and architecture](https://dynatrace-wwse.github.io/codespaces-framework)**
