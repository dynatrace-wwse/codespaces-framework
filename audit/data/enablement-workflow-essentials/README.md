# <img src="https://cdn.bfldr.com/B686QPH3/at/w5hnjzb32k5wcrcxnwcx4ckg/Dynatrace_signet_RGB_HTML.svg?auto=webp&format=pngg" alt="DT logo" width="45"> Enablement Workflow Essentials
[![Dynatrace](https://img.shields.io/badge/Dynatrace-Intelligence-purple?logo=dynatrace&logoColor=white)](https://dynatrace-wwse.github.io/codespaces-framework/dynatrace-integration/#mcp-server-integration)
[![Mastering](https://img.shields.io/badge/Mastering-Complexity-8A2BE2?logo=dynatrace)](https://dynatrace-wwse.github.io)
[![Downloads](https://img.shields.io/docker/pulls/shinojosa/dt-enablement?logo=docker)](https://hub.docker.com/r/shinojosa/dt-enablement)
[![Integration tests](https://github.com/dynatrace-wwse/enablement-workflow-essentials/actions/workflows/integration-tests.yaml/badge.svg)](https://github.com/dynatrace-wwse/enablement-workflow-essentials/actions)
[![Version](https://img.shields.io/github/v/release/dynatrace-wwse/enablement-workflow-essentials?color=blueviolet)](https://github.com/dynatrace-wwse/enablement-workflow-essentials/releases)
[![Commits](https://img.shields.io/github/commits-since/dynatrace-wwse/enablement-workflow-essentials/latest?color=ff69b4&include_prereleases)](https://github.com/dynatrace-wwse/enablement-workflow-essentials/graphs/commit-activity)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/dynatrace-wwse/enablement-workflow-essentials/blob/main/LICENSE)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-green)](https://dynatrace-wwse.github.io/enablement-workflow-essentials/)

___

author: Tony Pope-Cruz

dynatrace automation engine workflow essentials hands on lab

## Lab Overview

During this hands-on training, you will learn the foundation to be able to automate any relevant use case within Dynatrace using Workflow automations.

**Lab tasks:**

1. Parameters and Task Results

    - Parameter Task – define parameters/variables that will be used by subsequent tasks
    - Parameter Task Output – return parameters for subsequent tasks
    - Accessing Parameter Task Results – access the parameters from result of the task

2. Fetch API

    - HTTP GET (Dynatrace Status.io) – bring external data into Dynatrace workflows
    - HTTP POST (Webhook.Site) – send data to external systems from Dynatrace workflows

3. Looping Tasks

    - DQL Query – execute a DQL query that returns multiple records/rows
    - Looping Over Results – perform logic against each DQL record/row

4. Credential Vault

    - Access an API token from the Credential Vault using the Dynatrace SDK

5. Metric Ingest

    - Retrieve data from an external system using HTTP GET
    - Ingest retrieved data point using the Dynatrace API with API token (not optimal)
    - Ingest retrieved data point using the Dynatrace SDK (optimal)

6. Event Ingest

    - Query ingested metric data point using DQL
    - Evaluate metric value against a threshold
    - If the threshold is breached, generate an event using the Dynatrace SDK

7. Event Trigger

    - Trigger Workflow when a Davis problem event is detected
    - Events are filtered on the type of event, based on the previous exercise

8. Slack for Workflows
    
    - Third party integration is typically accomplished with Workflow Apps
    - The Slack for Workflows App provides an easy-to-use mechanism for sending messages to a Slack environment containing the data and context from Dynatrace
    - Leverage Jinja expressions to access Workflow action results and implement logic

9. Code Snippets

    - Review code snippets to re-use with Workflows in your custom use cases

10. Clean up

Ready to learn the essentials of automating your use cases with Dynatrace Workflows?

## [View the Lab Guide](https://dynatrace-wwse.github.io/enablement-workflow-essentials)