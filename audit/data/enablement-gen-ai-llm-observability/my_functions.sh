#!/bin/bash
# ======================================================================
#          ------- Custom Functions -------                            #
#  Space for adding custom functions so each repo can customize as.    # 
#  needed.                                                             #
# ======================================================================


IMAGE_NAME="ai-travel-advisor:local"
NAMESPACE="ai-travel-advisor"
DEPLOYMENT_NAME="ai-travel-advisor"

redeployApp(){

  printInfoSection "Building local image for $DEPLOYMENT_NAME"

  printInfo "docker build -t $IMAGE_NAME app"

  if ! docker build -t $IMAGE_NAME app; then
    printError "❌ Docker build failed. Stopping deployment, fix the compilation issues..."
    return 1
  else
    printInfo "✅ Docker build succeeded. New image built $IMAGE_NAME"
  fi

  printInfo "Loading image into kind cluster"
  kind load docker-image $IMAGE_NAME

  printInfo "Updating deployment image"
  kubectl set image deployment/$DEPLOYMENT_NAME $DEPLOYMENT_NAME=$IMAGE_NAME -n $NAMESPACE

  printInfo "Restarting deployment to apply changes"
  kubectl rollout restart deployment/$DEPLOYMENT_NAME -n $NAMESPACE

  printInfo "Waiting for rollout to complete"
  kubectl rollout status deployment/$DEPLOYMENT_NAME -n $NAMESPACE

  printInfo "✅ Done"

}