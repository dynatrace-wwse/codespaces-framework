#!/bin/bash
#loading functions to script
export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal

startKindCluster

installK9s

# Dynatrace Operator is deployed automatically, secrets are read from the env.
dynatraceDeployOperator

# You can deploy CNFS
deployCloudNative

deployAstroshopNew

# This step is needed, do not remove it
# it'll verify if there are error in the logs and will show them in the greeting as well a monitoring 
finalizePostCreation

printInfoSection "Your dev container finished creating"
