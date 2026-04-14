#!/bin/bash
#loading functions to script
export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal

verifyEnvironmentVars

startOtelCollector

setUpPythonEnv

startApp

initApp

finalizePostCreation

printInfoSection "Your dev container finished creating"