#!/bin/bash
#loading functions to script
export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal

# Kind note needed
#startKindCluster

#installK9s

finalizePostCreation

printInfoSection "Your dev container finished creating"
