#!/bin/bash
# ======================================================================
#          ------- Custom Functions -------                            #
#  Space for adding custom functions so each repo can customize as.    # 
#  needed.                                                             #
# ======================================================================

APP_DIR="$REPO_PATH/app"


customFunction(){
  printInfoSection "This is a custom function that calculates 1 + 1"

  printInfo "1 + 1 = $(( 1 + 1 ))"

}

APP_LOG="$APP_DIR/app.log"
APP_PID_FILE="$APP_DIR/app.pid"

startApp(){
  printInfoSection "Starting application"

  # Check if already running
  if [ -f "$APP_PID_FILE" ] && kill -0 "$(cat "$APP_PID_FILE")" 2>/dev/null; then
    printInfo "Application is already running (PID $(cat "$APP_PID_FILE"))."
    printAppInfo
    return 0
  fi

  printInfo "Launching Streamlit application in the background..."

  if [ -x "$REPO_PATH/.venv/bin/python3" ]; then
    local CMD="$REPO_PATH/.venv/bin/python3"
  elif command -v python3 >/dev/null 2>&1; then
    local CMD="python3"
  else
    printError "Could not find Python/Streamlit runtime. Run setUpPythonEnv first."
    return 1
  fi

  nohup "$CMD" -m streamlit run "$APP_DIR/app.py" < /dev/null > "$APP_LOG" 2>&1 &
  local PID=$!
  echo "$PID" > "$APP_PID_FILE"
  
  printAppInfo
}



printAppInfo(){
  printInfo "Application started with PID $PID"
  printInfo "Stop with: stopApp"
  printInfo "Logs with: logsApp"
  printInfo "Logs location: $APP_LOG"
} 

stopApp(){
  printInfoSection "Stopping application"

  if [ -f "$APP_PID_FILE" ]; then
    local PID
    PID=$(cat "$APP_PID_FILE")

    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      printInfo "Sent SIGTERM to process $PID"
      # Wait briefly, then force-kill if still alive
      sleep 2
      if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID"
        printWarn "Force-killed process $PID"
      fi
    else
      printInfo "Process $PID from PID file is not running."
    fi

    rm -f "$APP_PID_FILE"
    printInfo "Application stopped."
    return 0
  fi

  # No PID file found — look for orphaned streamlit processes
  printInfo "PID file not found. Searching for running Streamlit processes..."
  local PIDS
  PIDS=$(ps aux | grep '[s]treamlit' | awk '{print $2}')

  if [ -z "$PIDS" ]; then
    printInfo "No Streamlit processes found."
    return 0
  fi  

  printWarn "Found orphaned Streamlit process(es): $PIDS — killing them."
  for P in $PIDS; do
    kill "$P" 2>/dev/null
    sleep 1
    if kill -0 "$P" 2>/dev/null; then
      kill -9 "$P" 2>/dev/null
      printInfo "Force-killed process $P"
    else
      printInfo "Killed process $P"
    fi
  done
  printInfo "Application stopped."
}

logsApp(){
  if [ ! -f "$APP_LOG" ]; then
    printError "Log file not found: $APP_LOG"
    return 1
  fi
  less +F "$APP_LOG"
}

logsOtel(){
  if ! docker ps --format '{{.Names}}' | grep -q '^otel-collector$'; then
    printError "Container 'otel-collector' is not running."
    return 1
  fi
  docker logs -f otel-collector 2>&1 | less +F
}

initApp(){
  printInfoSection "Initializing application"

  if [ ! -f "$APP_LOG" ]; then
    printError "Log file not found: $APP_LOG — is the app running?"
    return 1
  fi

  printInfo "Triggering Streamlit app initialization..."

  # Determine which Python to use
  local PY
  if [ -x "$REPO_PATH/.venv/bin/python3" ]; then
    PY="$REPO_PATH/.venv/bin/python3"
  else
    PY="python3"
  fi

  # First wait for the HTTP server to be ready
  local RETRIES=0
  while [ "$RETRIES" -lt 10 ]; do
    if curl -s -o /dev/null -w '%{http_code}' http://localhost:8501/_stcore/health | grep -q "200"; then
      break
    fi
    sleep 1
    RETRIES=$((RETRIES + 1))
  done

  # Use the Streamlit AppTest API to trigger a headless script run
  # Capture output — AppTest prints to its own stdout, not to the Streamlit server log
  local INIT_OUTPUT
  INIT_OUTPUT=$("$PY" -c "
from streamlit.testing.v1 import AppTest

print('Running headless Streamlit session...')
at = AppTest.from_file('$APP_DIR/app.py', default_timeout=30)
at.run(timeout=30)
print('Headless session completed.')
" 2>&1) || true

  # Append the init output to the app log so everything is in one place
  echo "$INIT_OUTPUT" >> "$APP_LOG"

  if echo "$INIT_OUTPUT" | grep -q "Traceloop SDK initialized"; then
    printInfo "✅ Application initialized successfully — Traceloop SDK is ready."
  else
    printError "'Traceloop SDK initialized' not found in init output."
    printError "Init output:\n$INIT_OUTPUT"
    return 1
  fi
}

setUpPythonEnv(){

  printInfoSection "Setting up Python Environment"
  
  printInfo "Downloading & installing Python project manager Astral UV..."
  curl -LsSf https://astral.sh/uv/install.sh | sh

  if [ -f "$HOME/.local/bin/env" ]; then
    source "$HOME/.local/bin/env"
  fi
  
  printInfo "Creating and activating Python virtual environment..."
  # On container overlay filesystems (Codespaces/Docker), .venv/lib64 is a symlink.
  # 'uv venv --clear' calls rmdir() on it → EOVERFLOW (os error 75).
  # 'rm -rf' may also fail if the symlink blocks traversal, so unlink it explicitly first.
  if [ -d .venv ]; then
    find .venv -maxdepth 3 -type l -exec unlink {} \; 2>/dev/null || true
    rm -rf .venv 2>/dev/null || true
  fi
  uv venv --python 3.13 .venv
  source .venv/bin/activate

  printInfo "Installing Python dependencies..."
  # Increase UV HTTP timeout to handle slower connections
  export UV_HTTP_TIMEOUT=300
  # Suppress UV hardlink warning for cross-filesystem operations
  export UV_LINK_MODE=copy
  if ! uv pip install -r "$APP_DIR/requirements.txt"; then
    printError "Failed to install Python dependencies. "
    printError "Please check app/requirements.txt and try again."
    return 1
  fi

  printInfo "Updating Python configuration files..."
  python "$APP_DIR/update_config.py" build
}

startOtelCollector(){

  printInfoSection "Starting OpenTelemetry Collector"

  if [ -z "$OTEL_OTLP_ENDPOINT" ] || [ "$OTEL_OTLP_ENDPOINT" = "null" ]; then
    export OTEL_OTLP_ENDPOINT="http://localhost:4318"
  fi
  printInfo "Setting OTEL_OTLP_ENDPOINT=$OTEL_OTLP_ENDPOINT"
  echo "export OTEL_OTLP_ENDPOINT=\"$OTEL_OTLP_ENDPOINT\"" >> ~/.bashrc

  ##############################################################################
  # Start Otel Collector
  ##############################################################################
  printInfo "Starting up Otel Collector..."
  cd $REPO_PATH/otel
  ./start-otel.sh
  cd $REPO_PATH
}

verifyEnvironmentVars(){
  printInfoSection "Verifying Environment Variables"

  # Adjust to build, brev or local
  if [ -z "$APP_MODE" ] || [ "$APP_MODE" = "null" ]; then
    export APP_MODE=build
  fi

  # Validate APP_MODE is one of the allowed values
  if [ "$APP_MODE" != "local" ] && [ "$APP_MODE" != "brev" ] && [ "$APP_MODE" != "build" ]; then
    printError "APP_MODE must be 'local', 'brev', or 'build'."
    printError "Current value: $APP_MODE"
    return 1
  fi
  printInfo "APP_MODE is set to: $APP_MODE"

##############################################################################
# Setup Environment Variables
##############################################################################

# Adjust as required for using server to workshop environment variables.  
# Leave blank if not running a workshop
WORKSHOP_URL=
if [ "$WORKSHOP_URL" ] ; then
  printInfoSection "Getting workshop environment settings"

  # Verify that environment variables were retrieved successfully
  if [ -z "$WORKSHOP_PASSWORD" ] || [ "$WORKSHOP_PASSWORD" = "null" ]; then
    printError "WORKSHOP_PASSWORD is not set or is null."
    printError "Please check your WORKSHOP_PASSWORD and try again."
    return 1
  fi
  printInfo "WORKSHOP_PASSWORD detected."

  export DT_BASE_URL=$(curl -s -X POST $WORKSHOP_URL/dynatrace-url \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.dynatrace_url')

  export DT_API_TOKEN=$(curl -s -X POST $WORKSHOP_URL/dynatrace-token \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.dynatrace_api_token')

  export NVIDIA_API_KEY=$(curl -s -X POST $WORKSHOP_URL/nvidia-key \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.nvidia_api_key')

  export TAVILY_API_KEY=$(curl -s -X POST $WORKSHOP_URL/tavily-key \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.tavily_api_key')

  export OTEL_OTLP_ENDPOINT=$(curl -s -X POST $WORKSHOP_URL/otel-endpoint \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.otel_otlp_endpoint')

  export NVIDIA_MODEL_ENDPOINT_8001=$(curl -s -X POST $WORKSHOP_URL/nvidia-model-endpoint-8001 \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.nvidia_model_endpoint_8001')

  export NVIDIA_MODEL_ENDPOINT_8002=$(curl -s -X POST $WORKSHOP_URL/nvidia-model-endpoint-8002 \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.nvidia_model_endpoint_8002')

  export NVIDIA_MODEL_ENDPOINT_8003=$(curl -s -X POST $WORKSHOP_URL/nvidia-model-endpoint-8003 \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.nvidia_model_endpoint_8003')

  export NVIDIA_MODEL_ENDPOINT_8004=$(curl -s -X POST $WORKSHOP_URL/nvidia-model-endpoint-8004 \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$WORKSHOP_PASSWORD\"}" | jq -r '.nvidia_model_endpoint_8004')
fi

# get the NVIDIA model endpoints if in brev or local mode
if [ "$APP_MODE" = "brev" ] || [ "$APP_MODE" = "local" ]  ; then
  printInfoSection "Setting NVIDIA model endpoints for $APP_MODE mode"

  if [ -z "$NVIDIA_MODEL_ENDPOINT_8001" ] || [ "$NVIDIA_MODEL_ENDPOINT_8001" = "null" ]; then
    printError "Failed to retrieve NVIDIA_MODEL_ENDPOINT_8001."
    return 1
  fi
  printInfo "NVIDIA_MODEL_ENDPOINT_8001=$NVIDIA_MODEL_ENDPOINT_8001"
  printInfo "NVIDIA_MODEL_ENDPOINT_8002=$NVIDIA_MODEL_ENDPOINT_8002"
  printInfo "NVIDIA_MODEL_ENDPOINT_8003=$NVIDIA_MODEL_ENDPOINT_8003"
  printInfo "NVIDIA_MODEL_ENDPOINT_8004=$NVIDIA_MODEL_ENDPOINT_8004"
  echo "export NVIDIA_MODEL_ENDPOINT_8001=\"$NVIDIA_MODEL_ENDPOINT_8001\"" >> ~/.bashrc
  echo "export NVIDIA_MODEL_ENDPOINT_8002=\"$NVIDIA_MODEL_ENDPOINT_8002\"" >> ~/.bashrc
  echo "export NVIDIA_MODEL_ENDPOINT_8003=\"$NVIDIA_MODEL_ENDPOINT_8003\"" >> ~/.bashrc
  echo "export NVIDIA_MODEL_ENDPOINT_8004=\"$NVIDIA_MODEL_ENDPOINT_8004\"" >> ~/.bashrc
fi

##TODO: why adding the vars to .bashrc? for new terminals? we use zsh in the framework.
# get using the NVIDIA Build APIs, then get the API key
if [ "$APP_MODE" = "build" ] ; then
  printInfoSection "Setting NVIDIA API key"
  if [ -z "$NVIDIA_API_KEY" ] || [ "$NVIDIA_API_KEY" = "null" ]; then
    printError "Failed to retrieve NVIDIA_API_KEY."
    return 1
  fi
  echo "export NVIDIA_API_KEY=\"$NVIDIA_API_KEY\"" >> ~/.bashrc
fi

printInfoSection "Setting Dynatrace configuration"
printInfo "Setting Dynatrace DT_BASE_URL=$DT_BASE_URL"
if [ -z "$DT_BASE_URL" ] || [ "$DT_BASE_URL" = "null" ]; then
  printError "Failed to retrieve DT_BASE_URL."
  return 1
fi
if [ -z "$DT_API_TOKEN" ] || [ "$DT_API_TOKEN" = "null" ]; then
  printError "Failed to retrieve DT_API_TOKEN."
  return 1
fi
echo "export DT_BASE_URL=\"$DT_BASE_URL\"" >> ~/.bashrc
echo "export DT_API_TOKEN=\"$DT_API_TOKEN\"" >> ~/.bashrc

printInfoSection "Setting Tavily API key"
if [ -z "$TAVILY_API_KEY" ] || [ "$TAVILY_API_KEY" = "null" ]; then
  printError "Failed to retrieve TAVILY_API_KEY."
  return 1
fi
echo "export TAVILY_API_KEY=\"$TAVILY_API_KEY\"" >> ~/.bashrc



}