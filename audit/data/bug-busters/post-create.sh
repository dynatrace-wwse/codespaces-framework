#!/bin/bash
#loading functions to script
export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal

startKindCluster

installK9s

dynatraceDeployOperator

deployCloudNative

deployTodoApp

deployBugZapperApp

setLiveDebuggerVersionControlEnv

#deployDynatraceApp
printInfoSection "Deployment of the Dynatrace App"
printInfoSection "for deploying the Dynatrace App please make sure you have set DT_APP_OAUTH_CLIENT_ID and DT_APP_OAUTH_CLIENT_SECRET"
printInfoSection "then call the function 'deployDynatraceApp'"

finalizePostCreation

printInfoSection "Your dev container finished creating"
