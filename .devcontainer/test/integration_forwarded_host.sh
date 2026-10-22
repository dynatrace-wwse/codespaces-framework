#!/bin/bash
# What the APP behind the framework's k3d ingress-nginx receives for an
# Orbital-style app-window request (Host = the sslip ingress host,
# X-Forwarded-Host = the public window host, X-Forwarded-Proto = https).
#
# Dynatrace RUM picks the application host from X-Forwarded-Host first, so the
# backend must see the PUBLIC host there (D10). Runs both ways on one throwaway
# cluster -- the chart defaults (negative control: the header is overwritten
# with the sslip host) and the framework's values-k3d.yaml -- and fails unless
# each gives exactly the expected headers.
#
# Expected output (the last three lines):
#   PASS default      Host=todoapp.10.0.0.5.sslip.io X-Forwarded-Host=todoapp.10.0.0.5.sslip.io X-Forwarded-Proto=http
#   PASS framework    Host=todoapp--job1.example.com X-Forwarded-Host=todoapp--job1.example.com X-Forwarded-Proto=https
#   PASS no-xfh       Host=todoapp.10.0.0.5.sslip.io X-Forwarded-Host=todoapp.10.0.0.5.sslip.io X-Forwarded-Proto=https
#
# Needs docker, k3d, kubectl, helm. Touches only its own cluster (deleted on exit).
# Run from the repo root: bash .devcontainer/test/integration_forwarded_host.sh
set -euo pipefail

CLUSTER="${XFH_CLUSTER:-xfh-probe}"
PORT="${XFH_PORT:-18143}"
INGRESS_HOST="todoapp.10.0.0.5.sslip.io"
PUBLIC_HOST="todoapp--job1.example.com"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VALUES="$ROOT/.devcontainer/yaml/ingress/values-k3d.yaml"
VER="$(sed -n 's/^export INGRESS_NGINX_VERSION="\(.*\)"$/\1/p' "$ROOT/.devcontainer/util/variables.sh")"
CHART="4.${VER#1.}"
KUBECONFIG="$(mktemp)"
export KUBECONFIG
FAIL=0

cleanup() { k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true; rm -f "$KUBECONFIG"; }
trap cleanup EXIT

k3d cluster create "$CLUSTER" -p "${PORT}:80@loadbalancer" \
  --k3s-arg "--disable=traefik@server:0" --wait --timeout 180s >/dev/null
k3d kubeconfig get "$CLUSTER" > "$KUBECONFIG"

kubectl apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: echo}
spec:
  selector: {matchLabels: {app: echo}}
  template:
    metadata: {labels: {app: echo}}
    spec:
      containers:
      - {name: echo, image: "traefik/whoami:v1.10.3", ports: [{containerPort: 80}]}
---
apiVersion: v1
kind: Service
metadata: {name: echo}
spec: {selector: {app: echo}, ports: [{port: 80}]}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: todoapp}
spec:
  ingressClassName: nginx
  rules:
  - host: ${INGRESS_HOST}
    http:
      paths:
      - {path: /, pathType: Prefix, backend: {service: {name: echo, port: {number: 80}}}}
EOF
kubectl wait --for=condition=available deploy/echo --timeout=180s >/dev/null

install_ingress() {  # $@ = extra helm args
  helm upgrade --install ingress-nginx ingress-nginx \
    --repo https://kubernetes.github.io/ingress-nginx --version "$CHART" \
    --namespace ingress-nginx --create-namespace "$@" >/dev/null
  kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=240s >/dev/null
  sleep 5  # controller config reload after rollout
}

probe() {  # probe <label> <expected line> <curl header args...>
  local label="$1" want="$2" got; shift 2
  got="$(curl -s -H "Host: ${INGRESS_HOST}" "$@" "http://127.0.0.1:${PORT}/" \
    | tr -d '\r' | awk -F': ' '
        $1=="Host"{h=$2} $1=="X-Forwarded-Host"{x=$2} $1=="X-Forwarded-Proto"{p=$2}
        END{printf "Host=%s X-Forwarded-Host=%s X-Forwarded-Proto=%s", h, x, p}')"
  if [ "$got" = "$want" ]; then
    printf 'PASS %-12s %s\n' "$label" "$got"
  else
    printf 'FAIL %-12s got: %s\n     %-12s want: %s\n' "$label" "$got" "" "$want"
    FAIL=1
  fi
}

# Negative control: chart defaults (the framework's values minus controller.config).
install_ingress --set controller.admissionWebhooks.enabled=false
probe default "Host=${INGRESS_HOST} X-Forwarded-Host=${INGRESS_HOST} X-Forwarded-Proto=http" \
  -H "X-Forwarded-Host: ${PUBLIC_HOST}" -H "X-Forwarded-Proto: https"

# The framework's real values file.
install_ingress --values "$VALUES"
probe framework "Host=${PUBLIC_HOST} X-Forwarded-Host=${PUBLIC_HOST} X-Forwarded-Proto=https" \
  -H "X-Forwarded-Host: ${PUBLIC_HOST}" -H "X-Forwarded-Proto: https"
probe no-xfh "Host=${INGRESS_HOST} X-Forwarded-Host=${INGRESS_HOST} X-Forwarded-Proto=https" \
  -H "X-Forwarded-Proto: https"

exit "$FAIL"
