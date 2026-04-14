#!/bin/bash
#loading functions to script
export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal

# Kind not needed
#startKindCluster

finalizePostCreation

printInfoSection "Your dev container finished creating"
