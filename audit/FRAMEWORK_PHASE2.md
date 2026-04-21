
** Plan for Phase 2 of the Enablement Framework migration/refactor**
Please create a structured implementation plan for the points below, we want to test and make sure all repos work with the implemented changes. I'm applying for an IA prize within the company so help me use all best practices with what we have installed claude-code, gstack and dtctl for creating a great project that can be used worldwide for intern and external usage. We will be executing this one by one testing withtin the codespaces-framework hence extracting the core functionality and making the synch easier was the first step, now we modify the core functions and sync to the enablements easier. 

**Improvements for the core functionality**
**Improvement on App Exposure**
App exposure using ingress. The printGreeting shows how the app is exposed, this is done via NodePort. A better way is to use a magic domain like nip.io or alike. This way using an nginx ingress we can expose the apps replacing the functionality of exposing vi 30100, 30200 or 30300. We need to make sure this approach works also using github codespaces. The URL should be shown depending on the instantiation type in printGreeting. By using one port and subdomains makes the framework more versatile and easy to test. Also by exposing apps via NGINX and having it with Dynatrace monitored, we automatically monitor any app we expose via ingress with RUM.


**Deployment of Dynatrace operator, cloudnative fullstack, app only and ts components**
The dynakube has changed a lot recently. In the launch.md file of the remote-environment repo I explain how to adapt the dynakube so it can run in a single node k8s cluster such as kind. By default the ressources allocated to it are too big, we can even use only one Dynakube but I think is better to teach teh SEs to use two for a better separation of concerns. When the renablement has dynatrace it will use it by deploying deployApplicationMonitoring
or deployCloudNative. We need a better management of deploying Dynatrace components, analyze the functions dynatraceEvalReadSaveCredentials 
validateSaveCredentials deployCloudNative deployApplicationMonitoring dynatraceDeployOperator deployOperatorViaHelm, specially generateDynakube deployOperatorViaKubectl since we want to refactor this and do a better deployment of the DT components where we version the Operator version to make sure with time things dont break.

**Better management of Environment Variables**
The repos fail silently if no environment variables are set, codespaces does not show a failure if an ENV is not setted. We need to improve the error handling and how variables ara managed, hence in the audit table we are listing the variables listed in each repo so we can proper refactor. 
The function dynatraceEvalReadSaveCredentials should be completely refactored so we can evaluate any kind of variable and specific variables such as tenant and or token are validated like in the function validateSaveCredentials. Dynatrace with K8s monitoring normally needs DT_TENANT DT_OPERATOR_TOKEN DT_INGEST_TOKEN. DT_TENANT is how the tenant for API calls will be used, we ask the user to add DT_ENVIRONMENT like for prod https://abc123.apps.dynatrace.com or for sprint -> https://abc123.sprint.apps.dynatracelabs.com"
The function verifyParseSecret has this logic:
     Logic
     convert apps to live
     https://abc123.apps.dynatrace.com -> https://abc123.live.dynatrace.com 
     remove apps from string
     https://abc123.sprint.apps.dynatracelabs.com -> https://abc123.sprint.dynatracelabs.com 
     https://abc123.dev.apps.dynatracelabs.com -> https://abc123.dev.dynatracelabs.com 

This is because we ask the user to copy/paste it from the UI. The environment management does not need to be saved as a secret in kubectl, specially since we want to refactor the TIER image and some repos do not need or wont use k8s. 

I can envision a way to setup the post-create.sh file with a function such as variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:false
and it will validate if the environments variables are set, run a validattion for example the if contains DT*TOKEN we validate if it has a dynatrace token and if DT_ENVIRONMENT is set, verify if its prod, sprint or dev and will set intern variables such as DT_TENANT and DT_OTEL_ENDPOINT. 


**Unify the Astroshop deployments**
THe repos have different AStroshop or OpentelemetryApp deployments sparced thorughout time. We should differentiate between two apps when talking about Astroshop pr Opentelemetry. the Astroshop one which is the one defined in the deployApp astroshop (which is the one curated by the demo.live dynatrace team) and the Opentelemetry demo created by the CNCF. All other versions sparsed in the repos should use one or the other. The Opentelemetry can be fetched from my_functions file deployOpentelemetryDemo this one fetches the latest from upstream from the CNCF. There are repos such as the kubernetes or opentelemetry or logs I believe that use an outdated helm chart of the App. We should unify them and either they use the Astroshop or the Opentelemetry one. We should bring the Opentelemetry demo app into the core functionality so users using the framework can use that app and deploy/undeploy it via the deployApp function. 

**Improved error handling and monitoring***
ALl repos are monitored with Dynatrace.  More can be read here https://dynatrace-wwse.github.io/codespaces-framework/monitoring/ which is the monitoring.md file of the framework repo. The monitoring signals are in the COE https://geu80787.apps.dynatrace.com/ tenant where we have dtctl configured. The RUM apps are defined https://geu80787.apps.dynatrace.com/ui/apps/dynatrace.classic.web/#uemapplications;appsshown=webapps;gtf=-2h;gf=all and are setted as agentless apps https://geu80787.apps.dynatrace.com/ui/apps/dynatrace.classic.deploy.agentless/#install/apps;gf=all
in the audit table you can see that some apps or repos are not using a unique app id. We need to fix that so we can monitor all apps. 

For the error handling in the post-create.sh file the SECONDS variable is set so in finalizePostCreation we can measure the time it took to start the container creation.  The function verifyCodespaceCreation verifies the creation and sets up some variables that will be sent later via the function postCodespaceTracker. We need to enahnce the error payload so we send a string representing the errors why the repo failed.  The repo that handles the payload is codespaces-tracker which is deployed in aGKE cluster. this is the workload being monitored by dynatrace

https://geu80787.apps.dynatrace.com/ui/apps/dynatrace.kubernetes/smartscape/workload/K8S_WORKLOAD?perspective=Health&sort=healthIndicators%3Adescending&sidebarOpen=false&detailsId=K8S_DEPLOYMENT-EFF411DF46F82E68&detailsTab=Overview&tf=now-12h%3Bnow#filtering=%22Cluster%22+%3D+%22gke-whydevslovedynatrace-beta%22+AND+Namespace+%3D+%22codespaces-tracker%22

From the logfiles we have a pipeline defined 
https://geu80787.apps.dynatrace.com/ui/apps/dynatrace.settings/settings/openpipeline-logs/pipelines/pipeline_codespaces-tracker_5815?page=1&pageSize=50
In there we should add the string error. Also there are fields such as geo metadata that we fetch from the webserver in GKE so we can point from where intn the world the codespaces are being created. Event.Type is codespacestracker.creation and provider is com.dynatracewwse.codespacestracker

Task from improving the monitoring is in the code, but as well in Dynatrace. Enhance this dashboard 
https://geu80787.apps.dynatrace.com/ui/apps/dynatrace.dashboards/dashboard/041e6584-bdae-4fa0-9fa1-18731850cf20#from=now%28%29-30d&to=now%28%29
Verify the monitoring data, enhance it, add a worldmap and better visualizations.


**Improvements in all repos**
**Improve Integration tests for each repo**
We need to improve the integration.sh (integration tests of the repos) to make sure that they work, as an example see the live-debugger-bug-hunting repo. It has more mature tests and assertions that make sure that the repo works. The tests make sure that the different sections of the repo work. I want to implement this for all repos, so on every PR we can make sure that nothing breaks and that the enablement works. We will use the organisation secrets so we can monitor all repos. Goal for this is later to have self-service enablements with assertions and validations to make sure the student understand the tasks and we can give certifications or badges for the trainings done. We can test also locally when we do make start inside each repo inside the .devcontainer directory. Important is that when we run make start no docker container is running, for this we can implement a Make target called make clean-start where we kill and remove running docker images so we build and start a new environment. inside the environment we can run runIntegrationTest to validate if the integration.sh tests in codespaces work as we would like to.

** Migration from gen2 to gen3**
Now, within this task we want to emulate the user/student and do the enablement for them. From beginning to end following the documentation. we need to spot inconsistencies and things that are wrong or changed. We want to validate clarify in the explanation, if the documentation is missing or wrong. Be the role of a Dynatrace professor. We want to curate all the repos and make sure that they  actually work
But before that we need integration test and  to all repos using the core functions that we will change in the previous tasks. We also want to refactor some core functions so we have better varables management, better error handling and better installation of Dynatrace and better app exposure. 
