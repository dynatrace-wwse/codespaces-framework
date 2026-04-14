--8<-- "snippets/send-bizevent/index.js"

--8<-- "snippets/dt-enablement.md"

# Enablement Workflow Essentials

--8<-- "snippets/disclaimer.md"

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

## Technical Specification

### Technologies Used
- [Dynatrace](https://www.dynatrace.com/trial)
- [Slack](https://slack.com/)
- [Webhook.site](https://webhook.site/)

### Reference Architecture
N/A

## Continue

In the next section, we'll review the prerequisites for this lab.

<div class="grid cards" markdown>
- [Continue to getting started:octicons-arrow-right-24:](2-getting-started.md)
</div>
