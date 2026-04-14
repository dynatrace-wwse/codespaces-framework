# Codespaces
--8<-- "snippets/codespaces.js"

--8<-- "snippets/dt-enablement.md"

## 1. Launch Codespace

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://github.com/codespaces/new?hide_repo_select=true&ref=main&repo=1028024094&skip_quickstart=true){target="_blank"}


## 1.1 Codespaces configuration

!!! tip "Branch, Machine sizing & secrets"
    - Branch
        - select the **main** branch
    - Machine sizing
        - As a machine type select **4-core**
    - Secrets (enter your credentials within the following variables)
        - DT_ENVIRONMENT
        - DT_OPERATOR_TOKEN
        - DT_INGEST_TOKEN
        - DT_APP_OAUTH_CLIENT_ID
        - DT_APP_OAUTH_CLIENT_SECRET


## 2. Explore what has been deployed

Your Codespace has now deployed the following resources:

- A local Kubernetes ([kind](https://kind.sigs.k8s.io/){target="_blank"}) cluster monitored by Dynatrace, with some pre-deployed apps that will be used later in the demo.

- The Todo Java App and the Bugzapper Node.js app which will be used in our debugging quiz

- The Dynatrace Quiz App which will be available at https://yourTenantID.apps.dynatrace.com/ui/apps/my.bug.busters/

## 3. Tips & Tricks

### Navigating in your local Kubernetes cluster
The client `kubectl` is configured for you automatically.

### Exposing the apps to the public
The app TODO app and Bugzapper apps are being exposed from the devcontainer to your localhost or the github dns domain. If you want to make the endpoints publicly accesible, just go to the ports section in VsCode, right click on them and change the visibility to public.

## 4. Troubleshooting

If there is an issue with the application, we recommend you verify the health of the Kind cluster. 

```bash
kubectl cluster-info
```

Validate the pods are running successfully:
```sh
kubectl get pods -n bugzapper
```
```sh
kubectl get pods -n todoapp
```
```sh
kubectl get pods -n dynatrace
```

Now let's get started with the bug hunting. Click below to start.

<div class="grid cards" markdown>
- [Let's Find Some Bugs:octicons-arrow-right-24:](1-bugzappers-bugs.md)
</div>
