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

deployCronJobs() {
  printInfoSection "Deploying CronJobs for Astroshop for this lab"
  kubectl apply -f $REPO_PATH/.devcontainer/manifests/cronjobs.yaml
}