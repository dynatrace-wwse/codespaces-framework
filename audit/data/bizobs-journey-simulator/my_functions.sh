#!/bin/bash
# ======================================================================
#          ------- Custom Functions -------                            #
#  Space for adding custom functions so each repo can customize as.    # 
#  needed.                                                             #
# ======================================================================

LOG_FILE="$REPO_PATH/app/log/bizobs.log"
PID_FILE="$REPO_PATH/app/bizobs.pid"

customFunction(){
  printInfoSection "This is a custom function that calculates 1 + 1"

  printInfo "1 + 1 = $(( 1 + 1 ))"

}

installOneagent(){
  
#TODO: This wont work since its a container.

# Install OneAgent if DT_PAAS_TOKEN is available
if [ -n "$DT_PAAS_TOKEN" ] && [ -n "$DT_ENVIRONMENT" ]; then
    printInfoSection "Installing Dynatrace OneAgent..."
    
    # Download OneAgent installer
    if wget -O /tmp/Dynatrace-OneAgent-Linux.sh "${DT_ENVIRONMENT}/api/v1/deployment/installer/agent/unix/default/latest?arch=x86&flavor=default" \
        --header="Authorization: Api-Token ${DT_PAAS_TOKEN}" 2>/dev/null; then
        
        # Install OneAgent with proper permissions
        if sudo /bin/sh /tmp/Dynatrace-OneAgent-Linux.sh --set-app-log-content-access=true --set-infra-only=false 2>/dev/null; then
            printInfo "OneAgent installed successfully"
            # Clean up installer
            rm -f /tmp/Dynatrace-OneAgent-Linux.sh
        else
            printWarn "OneAgent installation failed (may require elevated permissions)"
            printWarn "BizEvents will not be captured without OneAgent"
        fi
    else
        printWarn "Could not download OneAgent installer"
        printWarn "Check DT_ENVIRONMENT and DT_PAAS_TOKEN configuration"
    fi
else
    printWarn "DT_PAAS_TOKEN not set - OneAgent will not be installed"
    printWarn "Configure GitHub Codespaces secret: DT_PAAS_TOKEN"
    printWarn "BizEvents will not be captured without OneAgent"
fi
}


installNodeDependencies(){
  printInfoSection "Getting the app ready, installing node dependencies..."
  # Install dependencies
  cd $REPO_PATH/app/
  npm install
  printInfo "installation finished"
}


startApp(){
  printInfoSection "Starting BizObs Journey Simulator 👔 🤖 📊"
  printInfo "writing log to $LOG_FILE"
  printInfo "writing PID to $PID_FILE"

  # Install dependencies
  cd $REPO_PATH/app/
  nohup npm start > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  printInfo "App started with PID $(cat $PID_FILE). Logs: $LOG_FILE"
  printInfo "For watching the log, type in the terminal 'log'"

}

stopApp(){
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    echo "Stopping process $PID..."
    kill "$PID" && rm "$PID_FILE"
    echo "Process stopped."
  else
    echo "No PID file found. Is the app running?"
  fi
}


log() {
    if [ -f "$LOG_FILE" ]; then
        less +F "$LOG_FILE"
    else
        printInfo "Log file not found: $LOG_FILE"
    fi
}



_updatePackage(){

  #TODO: Do we need this?
  # Update package.json scripts if needed
  if command -v jq &> /dev/null; then
    # Add development scripts to package.json
    jq '.scripts.dev = "node server.js" | .scripts.codespaces = "./quick-setup.sh"' package.json > package.json.tmp && mv package.json.tmp package.json
  fi
}