
# Dynatrace Codespaces Tracker

[![Build and Deploy to GKE](https://github.com/dynatrace-wwse/codespaces-tracker/actions/workflows/deploy.yaml/badge.svg)](https://github.com/dynatrace-wwse/codespaces-tracker/actions/workflows/deploy.yaml) [![Version](https://img.shields.io/github/v/release/dynatrace-wwse/codespaces-tracker?color=blueviolet)](https://github.com/dynatrace-wwse/codespaces-tracker/releases) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/dynatrace-wwse/codespaces-tracker/blob/main/LICENSE)

Spring Boot application running on GKE that receives JSON payloads from GitHub Codespaces instantiations. The payload is enriched with geo-location data (via MaxMind GeoLite) and logged. Dynatrace OneAgent picks up the logs and generates BizEvents for adoption analytics.

## Architecture

```
Codespace (post-create.sh)
  └─ POST /api/receive ─→ codespaces-tracker (GKE, 3 replicas)
                              ├─ Auth check (base64 token)
                              ├─ IP → MaxMind GeoLite City API
                              │    └─ Enriches: city, country, region, continent,
                              │       ISO code, latitude, longitude
                              └─ Logs enriched JSON → Dynatrace BizEvents
```

**Endpoint:** `https://codespaces-tracker.whydevslovedynatrace.com/api/receive`
**Namespace:** `codespaces-tracker`
**Monitored in:** [COE Dashboard](https://geu80787.apps.dynatrace.com/ui/apps/dynatrace.dashboards/dashboard/041e6584-bdae-4fa0-9fa1-18731850cf20)

## Geo Enrichment Fields

The tracker enriches every payload with geo-location data from the MaxMind GeoLite City API:

| Field | Type | Example | Used for |
|---|---|---|---|
| `geo.city.name` | string | "London" | City-level analytics |
| `geo.country.name` | string | "United Kingdom" | Country analytics |
| `geo.country.isoCode` | string | "GB" | Choropleth map (ISO 3166-1 alpha-2) |
| `geo.continent.name` | string | "Europe" | Continent grouping |
| `geo.region.name` | string | "England" | Region/state analytics |
| `geo.latitude` | double | 51.5074 | Bubble map visualization |
| `geo.longitude` | double | -0.1278 | Bubble map visualization |
| `client.ip` | string | "18.130.42.215" | Source IP |

These fields land in Dynatrace as `content.geo.*` in bizevents and power the world map visualizations in the dashboard.

## Local Development

```bash
# Run locally (requires MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY env vars)
mvn spring-boot:run

# Test payload (requires valid Authorization header)
curl -X POST http://localhost:8080/api/receive \
  -H "Content-Type: application/json" \
  -H "Authorization: <base64-encoded-token>" \
  -d '{"repository":{"name":"test-repo"},"codespace":{"type":"local"}}'
```

## Build and Deploy

### Automated (CI/CD)

Push to `main` triggers the GitHub Actions workflow that:
1. Builds the JAR with Maven
2. Builds and pushes a Docker image to `ghcr.io`
3. Deploys to the GKE cluster via `kubectl`
4. Runs a smoke test

### Manual

```bash
bash build-deploy.sh
```

## Kubernetes Resources

| Resource | File | Description |
|---|---|---|
| Deployment + Service | `k8s/deployment.yaml` | 3 replicas, ClusterIP on 8080 |
| Ingress | `k8s/ing-tls.yaml` | NGINX ingress with Let's Encrypt TLS |

### Required Secrets (in-cluster)

The `maxmind-credentials` secret must exist in the `codespaces-tracker` namespace:

```bash
kubectl create secret generic maxmind-credentials \
  --from-literal=MAXMIND_ACCOUNT_ID=<id> \
  --from-literal=MAXMIND_LICENSE_KEY=<key> \
  -n codespaces-tracker
```

### Required GitHub Secrets (for CI/CD)

| Secret | Description |
|---|---|
| `GKE_SA_KEY` | GCP Service Account JSON credentials |
| `GKE_CLUSTER_NAME` | GKE cluster name |
| `GKE_CLUSTER_LOCATION` | GKE cluster zone/region |

The Docker image is pushed as a **public** GitHub package — no image pull secret is needed in the cluster.
