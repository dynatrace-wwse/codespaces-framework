#!/bin/bash
#loading functions to script
export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal

startKindCluster

installK9s

# Nothing will be automated, engineers must install Dynatrace and Astroshop manually

finalizePostCreation

printInfoSection "Your dev container finished creating"
