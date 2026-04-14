
Dynatraces Github Codespaces Tracker

Simple SpringBoot application that runs in K8s for receiving JSON payloads that are sent from codespaces. The payload contains monitoring information that will be stored in Dynatrace as BizEvents so we in WWSE can understand adoption and friction of the Enablement Codespaces.

# How to run it locally
```bash
mvn spring-boot:run
```

🐳 How to crosscompile, push and deploy in k8s
```bash

bash build-deploy.sh

```
