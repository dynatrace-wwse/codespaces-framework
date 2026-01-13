Migration process for the astroshop.

Rename vars for deployment of Dynatrace Components

https://github.com/Dynatrace/opentelemetry-demo-gitops/blob/main/config/dt-operator/README.md

You need to set these env vars before deployment

CLUSTER_NAME - the name of the cluster displayed in the tenant
CLUSTER_API_URL - url of the tenant including the /api, e.g. https://wkf10640.live.dynatrace.com/api
OPERATOR_TOKEN - access token using the Kubernetes: Dynatrace Operator template
DATA_INGEST_TOKEN - access token using the Kubernetes: Data Ingest template



used repo https://github.com/Dynatrace/opentelemetry-demo-gitops
That repo should be a fork of the original one since argoCd is commiting daily due problem patterns.


