<!-- markdownlint-disable-next-line -->
# <img src="https://cdn.bfldr.com/B686QPH3/at/w5hnjzb32k5wcrcxnwcx4ckg/Dynatrace_signet_RGB_HTML.svg?auto=webp&format=pngg" alt="DT logo" width="30"> Bug Busters Bug Finding Expedition 📋

[![Mastering](https://img.shields.io/badge/powered_by-DT_enablement-8A2BE2?logo=dynatrace)](https://dynatrace-wwse.github.io/codespaces-framework/)
[![Downloads](https://img.shields.io/docker/pulls/shinojosa/dt-enablement?logo=docker)](https://hub.docker.com/r/shinojosa/dt-enablement)
[![Integration tests](https://github.com/dynatrace-wwse/bug-busters/actions/workflows/integration-tests.yaml/badge.svg)](https://github.com/dynatrace-wwse/bug-busters/actions)
[![Version](https://img.shields.io/github/v/release/dynatrace-wwse/bug-busters?color=blueviolet)](https://github.com/dynatrace-wwse/bug-busters/releases)
[![Commits](https://img.shields.io/github/commits-since/dynatrace-wwse/bug-busters/latest?color=ff69b4&include_prereleases)](https://github.com/dynatrace-wwse/bug-busters/graphs/commit-activity)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/dynatrace-wwse/bug-busters/blob/main/LICENSE)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-green)](https://dynatrace-wwse.github.io/bug-busters/)

___

Your mission in this bug finding expedition is to find bugs in the following two applications which are setup to run inside a local Kind Kubernetes cluster:

- BugZapper Asteroids Style Game (Node.js application)
- To-Do App (Java application)

As part of the journey, you'll utilize Dynatraces Live Debugger, Logs, and Traces to find where the bugs are occuring in the codebase.

When deploying the components inside this repository a Dynatrace App will also be deployed. This app is a quiz app which will ask you a set of multiple choice questions which you need to answer in the shortest time possible to achieve the highest score.

<p align="center">
    <img src="docs/img/bug-busters.jpg" alt="Bug Busters" width="500"/>
</p>

## Quickstart

Here are some short quickstart details to get going as you spin up the codespaces. More specific details are in the link at the bottom of the page.

1) To spin up the environment with GitHub codespaces, go to Codespaces and then select 'New with options' or directly by [clicking here](https://github.com/codespaces/new?hide_repo_select=true&ref=main&repo=1028024094&skip_quickstart=true)

You'll need:
 - A Dynatrace tenant endpoint which should look something like 'https://abc12345.live.dynatrace.com' 
    - Dynatrace Operator Token
    - Dynatrace Ingest Access Token with the following permissions:
        - metrics.ingest
        - logs.ingest
        - openTelemetryTrace.ingest
    - The above tokens can be generated easily from the Kubernetes app by clicking on Add Cluster -> Other Distributions -> Install Dynatrace Operator Section
    - An [OAuth Client](https://developer.dynatrace.com/develop/access-platform-apis-from-outside/#create-an-oauth-client) including the Client ID and Client Secret created from the Dynatrace Account settings to deploy the Dynatrace app. You'll need the following permissions:
        - app-engine:apps:install
        - app-engine:apps:run
        - app-engine:apps:delete (to uninstall the app if needed)

2) The codespace will automatically create a [Kind](https://kind.sigs.k8s.io/) Kubernetes cluster and deploy the BugZapper application and To-Do app. You can run

   ```sh
   kubectl get pods -n bugzapper
   ```
   ```sh
   kubectl get pods -n todoapp
   ```
   To see that the pods have spun up successfully. The Dynatrace One Agent should also be available:
   ```sh
   kubectl get pods -n dynatrace
   ```

Let's Get Started...

## [🧳🐞 Start the bug finding adventure here!](https://github.com/dynatrace-wwse/bug-busters)
