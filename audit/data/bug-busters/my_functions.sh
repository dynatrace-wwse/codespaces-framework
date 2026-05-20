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


setLiveDebuggerVersionControlEnv(){
  printInfoSection "Patching TODO Kubernetes deployment to set environment variables for the Live Debugger Version Control."
  bash app/patches/set_version_control.sh
}

deployDynatraceApp(){
  cd dt-app

  # get host from tenant URL
  export DT_HOST=$(echo $DT_TENANT | cut -d'/' -f3 | cut -d'.' -f1)

  # replace host in app config for Dynatrace App Deployment
  sed "s/ENVIRONMENTID/$DT_HOST/" app.config.json > tmpfile && mv tmpfile app.config.json

  CODESPACE_NAME=${CODESPACE_NAME}
  TODO_PORT=30100
  BUGZAPPER_PORT=30200

  printInfo "Updating Quiz questions with codespaces URLs."

  if [ -n "$CODESPACE_NAME" ]; then
    BUGZAPPER_URL="https://${CODESPACE_NAME}-${BUGZAPPER_PORT}.app.github.dev"
    TODO_URL="https://${CODESPACE_NAME}-${TODO_PORT}.app.github.dev"
  else
    BUGZAPPER_URL="http://localhost:30200"
    TODO_URL="http://localhost:30100"
  fi

  # Replace placeholders in quizData.ts to embed links in the Dynatrace app
  sed -e "s|{{BUGZAPPER_URL}}|${BUGZAPPER_URL}|g" \
    -e "s|{{TODO_URL}}|${TODO_URL}|g" \
    -e "s|{{ENVIRONMENT_ID}}|${DT_HOST}|g" \
    ui/app/data/quizData.ts > tmpfile && mv tmpfile ui/app/data/quizData.ts


  printInfo "Installing Dynatrace quiz app dependencies."
  #FIXME: Evaluate that this is installed.
  if command -v npm >/dev/null 2>&1; then
    printInfo "npm is installed"
  else
    printWarn "npm is not installed, installing it"
    installNpm
  fi

  npm install

  # deploy dynatrace app - note this will fail if the version in app.config.json has already been deployed
  printInfo "Deploying the Dynatrace app to $DT_TENANT"
  npx dt-app deploy

  cd ..
}

installNpm(){
  printInfoSection "Installing NodeJS and NPM"
  sudo apt update
  sudo apt install nodejs npm -y
}