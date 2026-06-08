#!/bin/bash
# Here is the definition of the test functions, the file needs to be loaded within the functions.sh file

assertDynatraceOperator(){

    printInfoSection "Testing Dynatrace Operator Deployment"
    kubectl get all -n dynatrace
    printWarn "TBD"
}

assertDynatraceCloudNative(){
    printInfoSection "Testing Dynatrace CloudNative FullStack deployment"
    kubectl get all -n dynatrace
    kubectl get dynakube -n dynatrace
    printWarn "TBD"
}

assertRunningApp(){
  # Assert an app is reachable via BOTH ingress hosts:
  #   1. magic-DNS public IP host: <app>.<detected-ip>.<MAGIC_DOMAIN>
  #   2. server hostname host:     <app>.<detected-hostname>
  # Probes localhost on the configured ingress port (K3D_LB_HTTP_PORT, default 80).
  # Required for parallel-worker testing where each worker's k3d binds to a
  # non-default port (e.g. 30080) and is reachable only via Host-header curl.
  local app_name="$1"
  local detected_ip detected_hostname port
  detected_ip=$(detectIP)
  detected_hostname=$(detectHostname)
  port="${K3D_LB_HTTP_PORT:-80}"

  local ip_host="${app_name}.${detected_ip}.${MAGIC_DOMAIN}"
  local name_host="${app_name}.${detected_hostname}"
  local target="http://localhost:${port}"

  printInfoSection "Testing app via ingress on ${target}"

  # Retry — ingress-nginx needs a moment to reconcile a freshly-created
  # Ingress resource. In parallel-worker runs the curl can race the
  # controller, so probe up to 8 times with 3s spacing per host.
  local h failed=0
  for h in "$ip_host" "$name_host"; do
    local ok=0 i
    for i in $(seq 1 8); do
      if curl --silent --fail --max-time 5 -H "Host: $h" "$target" > /dev/null; then
        printInfo "✅ App reachable via Host: $h on $target (attempt $i)"
        ok=1
        break
      fi
      sleep 3
    done
    if [[ "$ok" -eq 0 ]]; then
      printError "❌ App NOT reachable via Host: $h on $target after 8 attempts"
      failed=1
    fi
  done

  if [[ "$failed" -ne 0 ]]; then
    exit 1
  fi
}

getVscodeContainername(){
    docker ps --format '{{json .}}' | jq -s '.[] | select(.Image | contains("vsc")) | .Names'
    containername=$(docker ps --format '{{json .}}' | jq -s '.[] | select(.Image | contains("vsc")) | .Names')
    containername=${containername//\"/}
    echo "$containername"
}

assertRunningPod(){

  printInfoSection "Asserting running pods in namespace '$1' that contain the name '$2'"
  # Function to filter by Namespace and POD string, default is ALL namespaces
  # If 2 parameters then the first is Namespace the second is Pod-String
  # If 1 parameters then Namespace == all-namespaces the first is Pod-String
  if [[ $# -eq 2 ]]; then
    namespace_filter="-n $1"
    pod_filter="$2"
    verify_namespace=true
  elif [[ $# -eq 1 ]]; then
    namespace_filter="--all-namespaces"
    pod_filter="$1"
  fi

  # Need to check if the NS exists
  if [[ $verify_namespace == true ]]; then
    kubectl get namespace "$1" >/dev/null 2>&1
    if [[ $? -eq 1 ]]; then
      printError "❌ Namespace \"$1\" does not exists."
      exit 1
    fi
  fi

  # Get all pods, count and invert the search for not running nor completed. Status is for deleting the last line of the output
  CMD="kubectl get pods $namespace_filter 2>&1 | grep -c -E '$pod_filter'"
  printInfo "Verifying that pods in \"$namespace_filter\" with name \"$pod_filter\" are up and running."
  pods_running=$(eval "$CMD")
  
  if [[ "$pods_running" != '0' ]]; then
      printInfo "✅ \"$pods_running\" pods are running on \"$namespace_filter\" with name \"$pod_filter\"."    
  else 
      printError "❌ \"$pods_running\" pods are running on \"$namespace_filter\" with name \"$pod_filter\". "
      kubectl get pods $namespace_filter
      exit 1
  fi
}

assertDynakube(){
    printInfoSection "Verifying Dynakube is deployed and running"

}

assertRunningContainer(){
  # Assert a Docker container is running by name (or partial name)
  # Usage: assertRunningContainer <container-name>
  if [ -z "$1" ]; then
    printError "❌ assertRunningContainer: no container name provided"
    exit 1
  fi
  CONTAINER_NAME="$1"
  printInfoSection "Asserting Docker container '$CONTAINER_NAME' is running"

  running=$(docker ps --filter "name=$CONTAINER_NAME" --format '{{.Names}}' | grep -c "$CONTAINER_NAME")

  if [[ "$running" -gt 0 ]]; then
    printInfo "✅ Container '$CONTAINER_NAME' is running"
  else
    printError "❌ Container '$CONTAINER_NAME' is NOT running"
    docker ps -a --filter "name=$CONTAINER_NAME"
    exit 1
  fi
}

assertRunningHttp(){
  # Assert an HTTP endpoint is responding (200 OK)
  # Usage: assertRunningHttp <port> [path]
  if [ -z "$1" ]; then
    printError "❌ assertRunningHttp: no port provided"
    exit 1
  fi
  PORT="$1"
  PATH_SUFFIX="${2:-/}"
  URL="http://127.0.0.1:${PORT}${PATH_SUFFIX}"

  printInfoSection "Asserting HTTP endpoint $URL is responding"

  # Retry up to 5 times with 3s delay
  for i in $(seq 1 5); do
    if curl --silent --fail --max-time 5 "$URL" > /dev/null 2>&1; then
      printInfo "✅ HTTP $URL is responding"
      return 0
    fi
    printInfo "Attempt $i/5 — waiting 3s..."
    sleep 3
  done

  printError "❌ HTTP $URL is NOT responding after 5 attempts"
  exit 1
}

assertIngressRoute(){
  # Assert an Ingress resource exists for an app
  # Usage: assertIngressRoute <app-name> <namespace>
  local app_name="$1"
  local namespace="${2:-$1}"
  local ingress_name="${app_name}-ingress"

  printInfoSection "Asserting Ingress route '$ingress_name' exists in namespace '$namespace'"

  if kubectl get ingress "$ingress_name" -n "$namespace" &>/dev/null; then
    local host
    host=$(kubectl get ingress "$ingress_name" -n "$namespace" -o jsonpath='{.spec.rules[0].host}')
    printInfo "✅ Ingress '$ingress_name' exists with host: $host"
  else
    printError "❌ Ingress '$ingress_name' not found in namespace '$namespace'"
    exit 1
  fi
}

assertAppDeployed(){
  # Assert a full app stack: pod running + service exists + ingress route
  # Usage: assertAppDeployed <app-name> <namespace>
  local app_name="$1"
  local namespace="${2:-$1}"

  printInfoSection "Asserting full deployment of '$app_name'"

  # Check pod
  assertRunningPod "$namespace" "$app_name"

  # Check exposure — apps are reachable exclusively via ingress
  assertIngressRoute "$app_name" "$namespace"

  printInfo "✅ App '$app_name' fully deployed and accessible"
}

assertAstroshopContent() {
  # Validates the Astroshop frontend:
  #   1. Root path returns valid HTML
  #   2. Page contains expected shop-related keywords
  #   3. At least one static asset (img/css/js) loads with HTTP 200/304
  # Call after assertRunningApp confirms the ingress is up.
  local app_name="astroshop"
  local detected_ip port ip_host
  detected_ip=$(detectIP)
  port="${K3D_LB_HTTP_PORT:-80}"
  ip_host="${app_name}.${detected_ip}.${MAGIC_DOMAIN}"
  local target="http://localhost:${port}"

  printInfoSection "Asserting Astroshop HTML + assets via ${target} (Host: ${ip_host})"

  # Fetch main page with retries — Next.js SSR can be slow on first hit
  local html i
  for i in $(seq 1 8); do
    html=$(curl --silent --fail --max-time 20 -L -H "Host: ${ip_host}" "${target}/" 2>/dev/null)
    [[ -n "$html" ]] && break
    printInfo "Attempt ${i}/8 — page not ready, waiting 5s..."
    sleep 5
  done

  if [[ -z "$html" ]]; then
    printError "❌ Astroshop root page returned empty response after 8 attempts"
    exit 1
  fi

  # 1. Valid HTML structure
  if echo "$html" | grep -qi '<html'; then
    printInfo "✅ Root page returns valid HTML document"
  else
    printError "❌ Root page missing <html> tag"
    printError "First 300 chars: ${html:0:300}"
    exit 1
  fi

  # 2. Expected shop content keywords
  if echo "$html" | grep -qiE 'astro|opentelemetry|shop|telescope|astronomy|cart|product'; then
    printInfo "✅ Root page contains expected shop content"
  else
    printWarn "⚠️  Root page does not match expected shop keywords (may be a proxy/loading page)"
    printWarn "Page excerpt: $(echo "$html" | head -3)"
  fi

  # 3. Static asset reachability — extract src/href pointing to relative asset paths
  local assets asset_url status ok=0 fail=0
  assets=$(echo "$html" | \
    grep -oiE '(src|href)="(/[^"]+\.(png|jpg|jpeg|gif|webp|svg|css|js|woff2?))"' | \
    grep -oE '"[^"]+"' | tr -d '"' | sort -u | head -10)

  if [[ -z "$assets" ]]; then
    printWarn "No static asset URLs found in HTML — skipping asset HTTP checks"
  else
    while IFS= read -r asset_url; do
      status=$(curl --silent --output /dev/null \
        --write-out "%{http_code}" \
        --max-time 10 \
        -H "Host: ${ip_host}" \
        "${target}${asset_url}" 2>/dev/null)
      if [[ "$status" =~ ^(200|304)$ ]]; then
        printInfo "✅ ${asset_url} → HTTP ${status}"
        ok=$((ok + 1))
      else
        printWarn "⚠️  ${asset_url} → HTTP ${status}"
        fail=$((fail + 1))
      fi
    done <<< "$assets"

    if [[ "$ok" -gt 0 ]]; then
      printInfo "✅ ${ok} asset(s) loaded successfully (${fail} unexpected)"
    else
      printError "❌ No static assets loaded — checked ${fail} URLs, all failed"
      exit 1
    fi
  fi
}

assertEnvVariable(){
  # Assert an environment variable is set and optionally matches a pattern
  # Usage: assertEnvVariable <var-name> [pattern]
  local var_name="$1"
  local pattern="$2"
  local var_value=""
  var_value="$(eval "printf '%s' \"\${$var_name}\"")" 2>/dev/null

  printInfoSection "Asserting env variable '$var_name'"

  if [ -z "$var_value" ]; then
    printError "❌ Variable '$var_name' is not set"
    exit 1
  fi

  if [ -n "$pattern" ]; then
    if echo "$var_value" | grep -qE "$pattern"; then
      printInfo "✅ $var_name matches pattern '$pattern'"
    else
      printError "❌ $var_name='$var_value' does not match pattern '$pattern'"
      exit 1
    fi
  else
    printInfo "✅ $var_name is set"
  fi
}