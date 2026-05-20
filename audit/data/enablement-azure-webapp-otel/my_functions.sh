#!/bin/bash
# ======================================================================
#          ------- Custom Functions -------                            #
#  Space for adding custom functions so each repo can customize as.    # 
#  needed.                                                             #
# ======================================================================


installDotNet(){
  printInfo "Installing DotNet SDK Framework"
  sudo apt update
  sudo apt install -y dotnet-sdk-8.0
}


runWebapp(){

  printInfoSection "Running Dotnet App"
  PORT=5000

  # Calling function to parse out the OTEL endpoint and create a .live URL from the apps. URL.
  # TODO: Refactor this and do not assume K8s is running.
  dynatraceEvalReadSaveCredentials

  # Rename vars to keep consistency
  # We only need os DT_ENVIRONMENT & DT_INGEST_TOKEN

  # DT_API_URL -> DT_OTEL_ENDPOINT 
  # DT_API_TOKEN -> DT_INGEST_TOKEN
  cd $REPO_PATH/webapp/
  
  # Create logs directory if it doesn't exist
  mkdir -p $REPO_PATH/logs
  
  # Run dotnet in background and pipe output to log file
  dotnet run > $REPO_PATH/logs/webapp.log 2>&1 &
  printInfo "WebApp starting in background. Logs available at: $REPO_PATH/logs/webapp.log"
  printInfo "Process ID: $!"
  printInfo "For checking the logs type 'logsWebapp' in the Terminal"

  waitAppCanHandleRequests $PORT

}

logsWebapp(){
  less +F $REPO_PATH/logs/webapp.log
}

stopWebapp(){
  printInfoSection "Stopping Dotnet App"
  pkill dotnet

}

customFunction(){
  printInfoSection "This is a custom function that calculates 1 + 1"

  printInfo "1 + 1 = $(( 1 + 1 ))"

}