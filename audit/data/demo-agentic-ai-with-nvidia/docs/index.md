--8<-- "snippets/index.js"

--8<-- "snippets/disclaimer.md"


--8<-- "snippets/dt-enablement.md"


## Overview

This repository demonstrates how to build a secure, enterprise-grade AI agent is incapsulated within a simply Python application built using:

- [NVIDIA NeMo Agent Toolkit](https://docs.nvidia.com/nemo/agent-toolkit/) and 
- NVIDIA [NeMo Guardrails](https://github.com/NVIDIA/NeMo-Guardrails). 
- [streamlit](https://www.streamlit.io) open-source app framework

All of the observability telemetry of traces, logs, and metrics are collected using the [Dynatrace distribution of the OpenTelemetry Collector](https://docs.dynatrace.com/docs/ingest-from/opentelemetry/collector) for analysis within [Dynatrace](https://www.dynatrace.com).

This diagram below depicts the setup consisting of:

- Sample Python app - Used to generate prompts and send telemetry data to and OpenTelemetry Collector
- OpenTelemetry Collector - Configured to send telemetry data to Dynatrace OTLP APIs
- NVIDIA Build - free to use LLM models accessed via APIs and a Build API key
- Tavily - Uses as Agentic tool to search the internet and accessed via APIs and a Build API key
- Dynatrace - View and analyze OpenTelemetry metrics

![Selfguided setup](img/selfguided-setup.png){ width="50%";}



<div class="grid cards" markdown>
- [Yes! let's begin :octicons-arrow-right-24:](2-getting-started.md)
