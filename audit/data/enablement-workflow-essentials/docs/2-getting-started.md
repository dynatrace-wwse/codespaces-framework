--8<-- "snippets/send-bizevent/2-getting-started.js"

--8<-- "snippets/requirements.md"

## Prerequisites

You will need full administrator access to a Dynatrace SaaS tenant with a DPS license.

* Workflow Settings - Authorization
* Slack for Workflows
* Slack Bot (Slack App within Slack Workspace)
* Config Generator
* Data Generator

### Workflow Settings - Authorization

To run a workflow with your user as the actor (this is the default on workflow creation), you need to authorize the AutomationEngine to use permissions in your name. Note: The AutomationEngine authorization only works for permissions granted to your user.

In your Dynatrace environment, open the Workflows App.

Validate that the following permissions are granted to workflow executions.

![./img/01-workflow-authorization-settings.png](./img/01-workflow-authorization-settings.png)

Primary permissions:

[ x ] app-engine:apps:run - Grants permission to list and run apps and gives basic access to the Launcher

[ x ] app-engine:functions:run - Grants permission to use the function-executor

[ x ] app-settings:objects:read - Grants permission to read app settings objects belonging to the schema

Secondary permissions:

[ x ] select all

### Slack for Workflows

In your Dynatrace environment, search, locate, and open the Hub App.

Search for the App `Slack` and click on it.  Note, it used to be called `Slack for Workflows`.

![./img/01-hub-slack-workflows.png](./img/01-hub-slack-workflows.png)

Click on Install to install the App into your environment.

Close any notification prompts if necessary.

Validate that the App was installed into your environment.

![./img/01-slack-workflows-install.png](./img/01-slack-workflows-install.png)

### Slack Bot (Slack App within Slack Workspace)

This lab requires a Slack App within a Slack Workspace.  In order to use the Slack App, you'll need a Slack OAuth token in the format `xoxb-`.

If you already have the token, you will provide it in the next step (Config Generator).

If you do not already have a Slack App and token, follow the documentation steps to create one.

[Slack App Documentation](https://docs.dynatrace.com/docs/analyze-explore-automate/workflows/actions/slack#slack-app){target=_blank}

### Config Generator

In your Dynatrace environment, open the Workflows App.

Import the workflow 'Workflow Essentials - Config Generator' using the source file [Workflow Essentials - Config Generator](https://github.com/dynatrace-wwse/enablement-workflow-essentials/blob/main/assets/dynatrace/workflows/dt_wftpl_workflow_essentials_config_generator.yaml){target=_blank}

Workflows are imported using the Upload button.

![./img/01-import-config-generator.png](./img/01-import-config-generator.png)

Locate the `js_set_parameters` task.  Locate the code snippet for the Slack connection.

Set the `CONNECTION_TOKEN` value to the Slack App Bot User Token you have been provided (or have created).

![./img/01-config-generator-set-parameters.png](./img/01-config-generator-set-parameters.png)

Run the workflow and validate that it runs successfully.

![./img/01-config-generator-execution.png](./img/01-config-generator-execution.png)

As a result of the workflow, three configurations have been made to the Dynatrace environment:

1. A credential vault entry has been added containing a Dynatrace API token
    * Copy the credential vault ID from the result of the `js_create_credential` task and store it in your notes somewhere, it is needed in a future exercise
2. The allow list for outbound connections has been updated with the hosts/domains required for this lab
    * No action required, simply verify using screenshot below
3. A Slack Connection has been created called `workflow_essentials_demo`
    * No action required, simply verify using screenshot below

![./img/01-config-generator-results.png](./img/01-config-generator-results.png)

### Data Generator

In your Dynatrace environment, open the Workflows App.

Import the workflow 'Workflow Essentials - Data Generator' using the source file [Workflow Essentials - Data Generator](https://github.com/dynatrace-wwse/enablement-workflow-essentials/blob/main/assets/dynatrace/workflows/dt_wftpl_workflow_essentials_data_generator.yaml){target=_blank}

Workflows are imported using the Upload button.

![./img/01-import-data-generator.png](./img/01-import-data-generator.png)

No changes are required.  The workflow is scheduled to run every 5 minutes.

![./img/01-data-generator-workflow.png](./img/01-data-generator-workflow.png)

This workflow will generate bizevents representing a multi-stage business process.  This data will be used by the other workflows in the lab, removing the requirement to deploy OneAgents or ingest data through other mechanisms.

!!! warning "Write BizEvents Access"
    If the Workflow fails to generate BizEvents, indicated by HTTP 4XX errors in the action logs, then your account is likely missing the `storage:events:write` permission.  For more details, please see the documentation: [Business Analytics API - IAM Policy](https://docs.dynatrace.com/docs/observe/business-analytics/ba-api-ingest#iam-policy){target=_blank}

## Continue

In the next section, we'll utilize essential Workflow techniques.

<div class="grid cards" markdown>
- [Continue to Workflow Essentials:octicons-arrow-right-24:](03_workflow_essentials.md)
</div>
