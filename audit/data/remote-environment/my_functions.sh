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

deployOpentelemetryDemo(){

  NAMESPACE="opentelemetry-demo"

  printInfoSection "Installing latest Opentelemetry Demo in NS='$NAMESPACE' from https://opentelemetry.io/docs/demo/kubernetes-deployment/"

  helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
  
  helm install opentelemetry-demo open-telemetry/opentelemetry-demo --namespace $NAMESPACE --create-namespace

  getNextFreeAppPort true
  PORT=$(getNextFreeAppPort)
  if [[ $? -ne 0 ]]; then
    printWarn "Application can't be deployed"
    return 1
  fi

  printInfo "Change $NAMESPACE frontend service from ClusterIP to NodePort"
  
  kubectl patch service frontend-proxy --namespace=$NAMESPACE --patch='{"spec": {"type": "NodePort"}}'

  printInfo "Exposing the $NAMESPACE frontend-proxy in NodePort $PORT"

  kubectl patch service frontend-proxy --namespace=$NAMESPACE --type='json' --patch="[{\"op\": \"replace\", \"path\": \"/spec/ports/0/nodePort\", \"value\":$PORT}]"

  printInfo "$NAMESPACE deployed succesfully and should handle request in port $PORT"
  
  printWarn "$NAMESPACE is quite heavy and might take a while to schedule all pods $PORT"

}

undeployOpentelemetryDemo(){

  printInfoSection "Uninstalling Opentelemetry Demo"

  helm uninstall opentelemetry-demo --namespace opentelemetry-demo
  
  printInfo "Deleting namespace opentelemetry-demo"

  kubectl delete namespace opentelemetry-demo
}

exposeOnHttp(){

  printInfoSection "Routing traffic from port 30100 to HTTP (80) for convenience http://$PUBLIC_IP"
  
  sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 30100

}
