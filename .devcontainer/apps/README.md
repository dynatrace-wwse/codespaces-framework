

## Adding a submodule for the demo.live astroshop
git submodule add https://github.com/Dynatrace/opentelemetry-demo-gitops.git opentelemetry-demo-gitops

git submodule init

cd opentelemetry-demo-gitops

### Check specific Tag
git fetch --tags

### fetch a specific version
git checkout <commit-hash>

cd ..
git add opentelemetry-demo-gitops

### Add to main repo the fetched version
git commit -m "Add Dynatrace opentelemetry-demo-gitops submodule at version v0.1.0"

#!/bin/bash
# Script to add Dynatrace opentelemetry-demo-gitops as a submodule and lock to a specific version

REPO_URL="https://github.com/Dynatrace/opentelemetry-demo-gitops.git"
SUBMODULE_PATH="opentelemetry-demo-gitops"
VERSION="v0.1.0"  # Change this to your desired tag or commit

echo "Adding submodule..."
git submodule add $REPO_URL $SUBMODULE_PATH

echo "Initialising and updating submodule..."
git submodule init
git submodule update

echo "Checking out version $VERSION..."
cd $SUBMODULE_PATH
git fetch --tags
git checkout $VERSION
cd ..

echo "Staging and committing changes..."
git add $SUBMODULE_PATH
git commit -m "Add Dynatrace opentelemetry-demo-gitops submodule at version $VERSION"

echo "Done! Push your changes with: git push"


# Notes

[N] - Submodule is not a good idea due the synch of other repositories.

[ ] - Add code to apps path, exchange it with the past one.
[ ] - sed HELM values without commiting it (possible to add env vars?)

[ ] - Add DT customization (dashboards, settings, etc...)



add steps of:
- helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
- helm repo update
- Renderer 1st argument can only be "image-provider"
- by default dt-credentials not set, collector_tenant_token is not specified. This can create confusion since the endpoint is the otel endpoint for the collector with this credentials specified in the dynatrace-otelcol-dt-api-credentials in the collector_tenant_secret.yaml

          endpoint: "${env:DT_ENDPOINT}"
          headers:
            Authorization: "Api-Token ${env:DT_INGEST_TOKEN}"
    

  - even if the credentials are set, they are not taken in consideration.  
- Load generator, why does it have 2 replicas? when having only 2 users?
- where is the frontend proxy? how do you expose flagd and load balancer? 
- If no proxy, how do you add rum?
- Fraud-detection is sometimes in crashloopback (altough Kafka is there, why?)



In this docu is not clear 
  https://github.com/Dynatrace/opentelemetry-demo-gitops/blob/main/config/dt-operator/README.md

You need to set these env vars before deployment

CLUSTER_NAME - the name of the cluster displayed in the tenant
CLUSTER_API_URL - url of the tenant including the /api, e.g. https://wkf10640.live.dynatrace.com/api
OPERATOR_TOKEN - access token using the Kubernetes: Dynatrace Operator template
DATA_INGEST_TOKEN - access token using the Kubernetes: Data Ingest template


## Script for deploying it
```bash

#!/bin/bash

helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts

helm repo update

# 2) Build (fetch) the chart dependencies into ./charts/
# Step Needed
cd charts/astroshop
helm dependency build .

# Not needed since it'll be done from Renderer
# 3) Install/upgrade your release (namespace optional)
helm upgrade --install astroshop . -n astroshop --create-namespace
cd -

if [ $# -lt 1 ]
then
    RENDERER="./config/kustomize/base/helm-renderer"
else
    RENDERER="./config/kustomize/overlays/$1/helm-renderer"
    if [ ! -f $RENDERER ]
    then
        echo "There is no overlay for [$1]" >&2
        exit 1
    fi
fi

NAMESPACE="astroshop"

# Build chart dependencies if needed
helm dependency build ./charts/astroshop

# Steps missing Helm?
# Add tenant endpoint and token
# Add Kustomize
```bash
VERSION=v5.4.2
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
  ARCH=amd64
elif [ "$ARCH" = "aarch64" ]; then
  ARCH=arm64
fi

curl -LO "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/${VERSION}/kustomize_${VERSION}_linux_${ARCH}.tar.gz"

tar -xzf kustomize_${VERSION}_linux_${ARCH}.tar.gz
sudo mv kustomize /usr/local/bin/

```
# .devcontainer/apps/otel/config/helm-values/values.yaml

helm upgrade --install astroshop ./charts/astroshop \
    --create-namespace \
    --namespace "${NAMESPACE}" \
    --atomic \
    -f "./config/helm-values/values.yaml" \
    --post-renderer "$RENDERER"

# --- For testing ---
# helm template astroshop ./charts/astroshop \
#     --create-namespace \
#     --namespace "${NAMESPACE}" \
#     --atomic \
#     -f "./config/helm-values/test.yaml" \
#     --post-renderer "$RENDERER"

```
