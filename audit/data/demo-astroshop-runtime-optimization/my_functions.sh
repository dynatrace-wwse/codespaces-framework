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


deployMemoryProblem(){

  printInfoSection "Rolling out Memory Problem"
  cd $REPO_PATH/opentelemetry-demo/extras/azdo-integration/alpha-cluster/k8s/
  bash agent_do_rollout.sh -v 1.12.2 
  cd -
}

deployCpuProblem(){
  printInfoSection "Rolling out CPU Problem Patterm"
  cd $REPO_PATH/opentelemetry-demo/extras/azdo-integration/alpha-cluster/k8s/
  bash agent_do_rollout.sh -v 1.12.1   
  cd -
}

deployNplusOneProblem(){
  printInfoSection "Rolling out CPU Problem Patterm"
  cd $REPO_PATH/opentelemetry-demo/extras/azdo-integration/alpha-cluster/k8s/
  bash agent_do_rollout.sh -v 1.12.3   
  cd -
}




doLoadtest(){

printWarn "This funtion is broken, is a WIP"

# Check if jmeter is there if not install it
#Install JMETER (something like this)
sudo apt update
sudo apt install jmeter -y

# go to directory
cd $REPO_PATH/opentelemetry-demo/extras/jmeter

# In  there you have the jmeter and a bash that triggers the AZDO Test, adapt them.
# Run Jmeter (check the binary installed) 
# Parametrize your command
/opt/jmeter/apache-jmeter-5.5/bin/jmeter -n -t $JMX_FILE -JSERVER_URL=$SERVER_URL -JVUCount=$VU -JLoopCount=$LOOPS  -l testreport.jtl

}
