#!/bin/bash
# ======================================================================
#          ------- Custom Functions -------                            #
#  Space for adding custom functions so each repo can customize as.    # 
#  needed.                                                             #
# ======================================================================

customFunction(){
  printInfoSection "This is a custom function that calculates 1 + 1"

  printInfo "1 + 1 = $(( 1 + 1 ))"

}

exposeUnguard(){
  
  getNextFreeAppPort true
  PORT=$(getNextFreeAppPort)
  if [[ $? -ne 0 ]]; then
    printWarn "Application can't be deployed"
    return 1
  fi

  #PORT=30100
  kubectl patch service unguard-envoy-proxy --namespace=unguard --patch="{\"spec\": {\"type\": \"NodePort\", \"ports\": [{\"port\": 8080, \"nodePort\": $PORT }]}}"
  #PORT2=30200
  #kubectl patch service unguard-frontend --namespace=unguard --patch="{\"spec\": {\"type\": \"NodePort\", \"ports\": [{\"port\": 80, \"nodePort\": $PORT2 }]}}"
}

deployUnguard(){

  printInfoSection "Deploying Unguard"
  getNextFreeAppPort true
  PORT=$(getNextFreeAppPort)
  if [[ $? -ne 0 ]]; then
    printWarn "Application can't be deployed, all NodePorts are busy"
    return 1
  fi

  if [[ "$ARCH" != "x86_64" ]]; then
    printWarn "This version of the Unguard only supports AMD/x86 architectures and not ARM, exiting deployment..."
    return 1
  fi
  

  printInfo "Unguard repository https://github.com/dynatrace-oss/unguard/"
  printInfo "A copy of the source files -> /unguard"

  printInfo "Adding bitnami chart ..."
  helm repo add bitnami https://charts.bitnami.com/bitnami

  printInfo "Installing unguard-mariadb ..."
  #helm install unguard-mariadb bitnami/mariadb --version 12.0.2 --set primary.persistence.enabled=false --wait --namespace unguard --create-namespace

  helm install unguard-mariadb bitnami/mariadb \
  --version 11.5.7 \
  --set primary.persistence.enabled=false \
  --set image.repository=bitnamilegacy/mariadb \
  --namespace unguard --create-namespace

  printInfo "Installing Unguard"
  helm install unguard  oci://ghcr.io/dynatrace-oss/unguard/chart/unguard --version 0.12.0 --namespace unguard 

  kubectl patch service unguard-envoy-proxy --namespace=unguard --patch="{\"spec\": {\"type\": \"NodePort\", \"ports\": [{\"port\": 8080, \"nodePort\": $PORT }]}}"

}

undeployUnguard() {

  printInfoSection "Undeploying Unguard"

  helm uninstall unguard -n unguard
  helm uninstall unguard-mariadb -n unguard

}


writeMcpJsonConfig(){
  local mcp_json_path="${REPO_PATH}/.vscode/mcp.json"
  
  printInfo "Writing MCP configuration to $mcp_json_path"
  
  # Create .vscode directory if it doesn't exist
  mkdir -p "${REPO_PATH}/.vscode"
  
  # Write the mcp.json file with the selected server configuration
  cat > "$mcp_json_path" <<EOF
{
	"servers": {
		"dynatrace-mcp-server": {
            "url": "https://${MCP_SERVER}-mcp-server.whydevslovedynatrace.com",
            "type": "http",
			"headers": {
        	"Authorization": "Basic ${MCP_AUTH_TOKEN}"
      		}
		}
	}
}
EOF

  if [ $? -eq 0 ]; then
    printInfo "✅ MCP configuration file written successfully"
  else
    printWarn "❌ Failed to write MCP configuration file"
    return 1
  fi
}

selectMcpServer(){
  printWarn "The COE MCP Remote Servers have been decommisioned since SSO is available for you to use, Github Codespaces also supported!"
  printInfo "More information here: https://dynatrace-wwse.github.io/codespaces-framework/dynatrace-integration/#mcp-server-integration"
  printInfoSection "Calling 'selectDemoEnvironment' instead"
  selectDemoEnvironment
}

selectMcpServer_deprecated(){
  printInfoSection "🧠 Please select the MCP Server you want to connect to:"
  printInfo "1. playground (wkf10640)"
  printInfo "2. demo.live (guu84124)"
  printInfo "3. tacocorp (bwm98081)"
  printf "Enter your choice (1-3): "
  read choice
  case $choice in
    1)
      MCP_SERVER="playground"
      ;;
    2)
      MCP_SERVER="demo.live"
      ;;
    3)
      MCP_SERVER="tacocorp"
      ;;
    *)
      echo "Invalid choice. Defaulting to playground."
      MCP_SERVER="playground"
      ;;
  esac
  export MCP_SERVER

  printInfo "Selected MCP Server: $MCP_SERVER"
  
  printf "Enter password for $MCP_SERVER: "
  read -s MCP_PASSWORD
  echo ""  # New line after password input

  MCP_AUTH_TOKEN=$(printf "%s" "devlove:$MCP_PASSWORD" | base64)
  export MCP_AUTH_TOKEN

  # Write the configuration to mcp.json
  writeMcpJsonConfig

  printInfoSection "$MCP_SERVER selected, the VS Code agent should start the MPC server automatically"
  printInfo "you can alternatively go to 'Extensions > MCP Servers installed > dynatrace-mcp-server' and start it."
  printInfo "If you want to connect to another MCP server, just type the function 'selectMcpServer'"

}
