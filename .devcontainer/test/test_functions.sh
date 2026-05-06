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
  # The 1st argument is the port.
  if [ -z "$1" ]; then
    PORT=30100
  else
    PORT=$1
  fi

  URL="http://127.0.0.1:$PORT"
  printInfoSection "Testing Deployed app running in $URL"

  # NodePorts are exposed differently per cluster engine:
  #   - kind: bound only inside the kind-control-plane container, so we curl from there
  #   - k3d:  forwarded to the host via k3d's port mapping → curl directly
  # Detect which is running and route accordingly.
  if docker ps --format '{{.Names}}' | grep -qx 'kind-control-plane'; then
    printInfo "Asserting app via 'docker exec kind-control-plane' on $URL"
    CMD=(docker exec kind-control-plane sh -c "curl --silent --fail $URL")
  else
    printInfo "Asserting app via host curl on $URL (k3d/host networking)"
    CMD=(curl --silent --fail "$URL")
  fi

  if "${CMD[@]}" > /dev/null; then
    printInfo "✅ App is running on $URL"
  else
    printError "❌ App is NOT running on $URL"
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
  # Assert a full app stack: pod running + service exists + ingress route (or NodePort)
  # Usage: assertAppDeployed <app-name> <namespace> [port]
  local app_name="$1"
  local namespace="${2:-$1}"
  local port="$3"

  printInfoSection "Asserting full deployment of '$app_name'"

  # Check pod
  assertRunningPod "$namespace" "$app_name"

  # Check exposure method
  if [[ "$USE_LEGACY_PORTS" == "true" && -n "$port" ]]; then
    assertRunningApp "$port"
  elif [[ "$USE_LEGACY_PORTS" != "true" ]]; then
    assertIngressRoute "$app_name" "$namespace"
  fi

  printInfo "✅ App '$app_name' fully deployed and accessible"
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