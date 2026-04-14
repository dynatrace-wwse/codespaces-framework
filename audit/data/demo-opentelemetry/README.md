<!-- markdownlint-disable-next-line -->
# <img src="https://cdn.bfldr.com/B686QPH3/at/w5hnjzb32k5wcrcxnwcx4ckg/Dynatrace_signet_RGB_HTML.svg?auto=webp&format=pngg" alt="DT logo" width="30"> <img src="https://opentelemetry.io/img/logos/opentelemetry-logo-nav.png" alt="OTel logo" width="45"> Codespaces OpenTelemetry Demo

[![Dynatrace](https://img.shields.io/badge/Dynatrace-Intelligence-purple?logo=dynatrace&logoColor=white)](https://dynatrace-wwse.github.io/codespaces-framework/dynatrace-integration/#mcp-server-integration)
[![Mastering](https://img.shields.io/badge/Mastering-Complexity-8A2BE2?logo=dynatrace)](https://dynatrace-wwse.github.io)
[![Downloads](https://img.shields.io/docker/pulls/shinojosa/dt-enablement?logo=docker)](https://hub.docker.com/r/shinojosa/dt-enablement)
[![Integration tests](https://github.com/dynatrace-wwse/demo-opentelemetry/actions/workflows/integration-tests.yaml/badge.svg)](https://github.com/dynatrace-wwse/demo-opentelemetry/actions)
[![Version](https://img.shields.io/github/v/release/dynatrace-wwse/demo-opentelemetry?color=blueviolet)](https://github.com/dynatrace-wwse/demo-opentelemetry/releases)
[![Commits](https://img.shields.io/github/commits-since/dynatrace-wwse/demo-opentelemetry/latest?color=ff69b4&include_prereleases)](https://github.com/dynatrace-wwse/demo-opentelemetry/graphs/commit-activity)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/dynatrace-wwse/demo-opentelemetry/blob/main/LICENSE)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-green)](https://dynatrace-wwse.github.io/demo-opentelemetry/)

___

Sample repository deploying the OpenTelemetry Astronomy Shop, a microservice-based distributed system intended to illustrate the implementation of OpenTelemetry in a near real-world environment.

The Application is being deployed using the helm approach described here

https://opentelemetry.io/docs/demo/kubernetes-deployment/

Using codespaces the app requires at least a machine with 4 core.

The following services will be available at these paths after the frontend-proxy service is exposed using NodePort 30100

- Webstore                http://localhost:30100/
- Jaeger UI               http://localhost:30100/jaeger/ui/
- Grafana                 http://localhost:30100/grafana/
- Load Generator UI       http://localhost:30100/loadgen/
- Feature Flags UI        http://localhost:30100/feature/

<p align="center">
<img src="docs/img/demo_screenshot.png" alt="Alt text" width="800"/>
</p>

## [👨‍🏫 Learn more about the OpenTelemetry Demo in Codespaces!](https://dynatrace-wwse.github.io/demo-opentelemetry)
