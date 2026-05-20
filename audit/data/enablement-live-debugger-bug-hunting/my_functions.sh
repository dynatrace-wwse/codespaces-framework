#!/bin/bash
# ======================================================================
#          ------- Custom Functions -------                            #
#  Space for adding custom functions so each repo can customize as.    # 
#  needed.                                                             #
# ======================================================================


### Variables
# Framework 1.3.0 exposes apps via nginx ingress (hostname-routed) instead of
# nodePort 30100. Reach the app through localhost:${K3D_LB_HTTP_PORT:-80} with
# a Host header that matches the ingress registered by registerApp/deployTodoApp.
APPLICATION_URL="http://localhost:${K3D_LB_HTTP_PORT:-80}"
APPLICATION_HOST="todoapp.$(detectHostname)"
IMAGE_NAME="todoapp:local"
NAMESPACE="todoapp"
DEPLOYMENT_NAME="todoapp"


customFunction(){
  printInfoSection "This is a custom function that calculates 1 + 1"

  printInfo "1 + 1 = $(( 1 + 1 ))"

}

redeployApp(){

  printInfoSection "Building local image"

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

  printInfo "Done"

}


_check_bug1(){

  addTask '{"title":"Clear completed task","completed":true}'
  
  clearCompletedTasks

}

is_bug1_there(){
  
  kubectl logs -l app=todoapp -c todoapp -n todoapp | grep 'completed=true' > /dev/null
  mark_completed=$?
  
  kubectl logs -l app=todoapp -c todoapp -n todoapp | grep 'Failed to delete completed todos' > /dev/null
  click_clear_completed=$?

  if [ $mark_completed -eq 0 ] && [ $click_clear_completed -eq 0 ]; then
    printInfo "🪲 Bug 'Clear completed' is there! Thanks for adding tasks and trying to clear them."
    return 0
  else
    printWarn " ⚠️ Please add a couple of task, mark them completed and then click on the 'clear completed' button in order to see if the 🪲 bug is there..."
    return 1
  fi

}


is_bug1_solved(){

  printInfoSection "Verifying if the 🪲 Bug 'Clear completed' is gone"
  
  _check_bug1

  kubectl logs -l app=todoapp -c todoapp -n todoapp | grep 'Removed Todo record.*completed=true' > /dev/null
  found_removed=$?

  if [ $found_removed -eq 0 ]; then
    printInfo "✅ Bug clear completed is gone."
    return 0
  else
    printWarn "⚠️ Bug clear completed is still there, tip: check the Arrays"
    return 1
  fi

}

is_bug2_solved(){
  printInfoSection "Verifying if the 🪲 Bug 'Special Characters' is gone"
  RC=0

  addTask '{"title":"Exciting validation!?#","completed":false}'

  printInfo "Retrieving todos to verify the title..."
  
  response=$(curl -s -H "Host: $APPLICATION_HOST" -X GET $APPLICATION_URL/todos)

  # Check if special characters are preserved
  if echo "$response" | grep -q '"title":"Exciting validation!?#"'; then
    printInfo "✅ Bug special characters is gone. Title preserved correctly."      
    RC=0
  elif echo "$response" | grep -q '"title":"Exciting validation"'; then
    printWarn "⚠️ Bug special characters is still there. Special characters were stripped from the title. Tip: Less is more ;)"
    RC=1
  fi

  # Delete taskId
  #deleteTask $response  
  return $RC
}

is_bug3_solved(){
  printInfoSection "Verifying if the 🪲 Bug 'Duplicate Task' is gone"
  RC=0

  # Generate a random 6-character ID
  random_id=$(head /dev/urandom | LC_ALL=C tr -dc 'a-zA-Z0-9' | head -c 6)
  title="Duplicated task with ID $random_id"

  addTask '{"title":"'"$title"'","completed":false}'
  
  response=$(curl -s -H "Host: $APPLICATION_HOST" -X GET $APPLICATION_URL/todos)
  task_id=$(echo "$response" | grep -o '"title":"'"$title"'","id":"[^"]*"' | grep -o '"id":"[^"]*"' | cut -d'"' -f4)

  if [ -n "$task_id" ]; then
    printInfo "Duplicating task with ID: $task_id"
    dup_response=$(curl -s -H "Host: $APPLICATION_HOST" -X POST $APPLICATION_URL/todos/dup/$task_id)
    
    if echo "$dup_response" | grep -q '"status":"ok"'; then
      # Get todos again to verify the duplicate
      response=$(curl -s -H "Host: $APPLICATION_HOST" -X GET $APPLICATION_URL/todos)
      
      # Count how many tasks have the correct title
      count=$(echo "$response" | grep -o '"title":"'"$title"'"' | wc -l)
      
      # Verify original task still exists with the same ID
      original_exists=$(echo "$response" | grep -q '"title":"'"$title"'","id":"'"$task_id"'"' && echo "yes" || echo "no")
      
      # Check if we have 2 tasks with the correct title and the original still exists
      if [ "$count" -eq 2 ] && [ "$original_exists" = "yes" ]; then
        printInfo "✅ Bug duplicate task is gone. Found 2 tasks with correct title '$title', including original with ID: $task_id"
        RC=0
      else
        printWarn "⚠️ Bug duplicate task is still there. Expected 2 tasks with title '$title', found: $count. Tip: Check the setter methods in duplicateTodo."
        RC=1
      fi
    else
      printInfo "❌ Failed to execute duplicate"
      RC=1
    fi
  else
    printInfo "❌ Could not find the task to duplicate"
    RC=1
  fi

  return $RC
}

deleteTask(){
  response="$1"
  # might need it later
  task_id=$(echo "$response" | grep -o '"title":"Exciting validation[^"]*","id":"[^"]*"' | grep -o '"id":"[^"]*"' | cut -d'"' -f4)
  if [ -n "$task_id" ]; then
    printInfo "Deleting task ID: $task_id"
    delete_response=$(curl -s -H "Host: $APPLICATION_HOST" -X DELETE $APPLICATION_URL/todos/$task_id)
    if echo "$delete_response" | grep -q '"status":"ok"'; then
      printInfo "Task deleted successfully"
    else
      printInfo "Failed to delete task"
    fi
  fi
}

solve_bug1(){
  
  printInfoSection "Solving the 🪲 Bug Clear Completed"

  _solve_bug "solution/bug1"

  printInfo "Now add some tasks, mark them completed and click on 'clear completed'"
  printInfo "You can assert that the bug is gone also by typing 'is_bug1_solved'"
}

solve_bug2(){

  printInfoSection "Solving the 🪲 Bug Special Characters"
  _solve_bug "solution/bug2"

  printInfo "Now add some tasks with special characters and see if they are added correctly"
  printInfo "You can assert that the bug is gone also by typing 'is_bug2_solved'"
}

solve_bug3(){

  printInfoSection "Solving the 🪲 Bug Duplicated Task"
  _solve_bug "solution/bug3"

  printInfo "Now duplicate a record and see if the duplicate is correct"
  printInfo "You can assert that the bug is gone also by typing 'is_bug3_solved'"
}

_solve_bug(){
  
  solution_branch=$1

  printInfo "Changing to the branch $solution_branch where the developer already added the solution for us"

  printInfo "git checkout $solution_branch"

  git checkout $solution_branch

  printInfo "then we compile the application and redeploy it to the Kubernetes Cluster using the function 'redeployApp'"
  
  redeployApp

  setVersionControl "$solution_branch"

}


setVersionControl(){
  printInfo "Setting version control in the DEPLOYMENT_NAME=$DEPLOYMENT_NAME to point to '$1' "
  updateVersionControl "$1" 
  patchDeployment

}


updateVersionControl(){

  version="$1"

  if [ -z "$version" ]; then
    version="v1.0.1"
  fi
  DT_LIVEDEBUGGER_REMOTE_ORIGIN=$(git remote get-url origin)
  DT_LIVEDEBUGGER_COMMIT=$(git rev-parse $version)

  printInfo "Fetching git revision for $version in DT_LIVEDEBUGGER_REMOTE_ORIGIN=$DT_LIVEDEBUGGER_REMOTE_ORIGIN" 
  printInfo "DT_LIVEDEBUGGER_COMMIT=$DT_LIVEDEBUGGER_COMMIT"

  export DT_LIVEDEBUGGER_REMOTE_ORIGIN=$DT_LIVEDEBUGGER_REMOTE_ORIGIN
  export DT_LIVEDEBUGGER_COMMIT=$DT_LIVEDEBUGGER_COMMIT
}

patchDeployment(){ 
kubectl patch deployment $DEPLOYMENT_NAME -n $NAMESPACE -p "$(cat <<EOF
{
    "spec": {
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "$DEPLOYMENT_NAME",
                        "env": [
                            {
                                "name": "DT_LIVEDEBUGGER_COMMIT",
                                "value": "$DT_LIVEDEBUGGER_COMMIT"
                            },
                            {
                                "name": "DT_LIVEDEBUGGER_REMOTE_ORIGIN",
                                "value": "$DT_LIVEDEBUGGER_REMOTE_ORIGIN"
                            }
                        ]
                    }
                ]
            }
        }
    }
}
EOF
)"
}


addTask(){
  jsontask="$1"
  if [ -z "$jsontask" ]; then
    jsontask='{"title":"Completed task","completed":true}'
  fi
  printInfo "adding task $jsontask"
  response=$(curl -s -H "Host: $APPLICATION_HOST" -X POST $APPLICATION_URL/todos \
    -H "Content-Type: application/json" -d "$jsontask")
  
  if echo "$response" | grep -q '"status":"ok"'; then
    printInfo "✅ Task added successfully"
  else
    printInfo "❌ Failed to add task"
  fi
}


clearCompletedTasks(){
  printInfo "Clearing completed Tasks"
  response=$(curl -s -H "Host: $APPLICATION_HOST" -X DELETE $APPLICATION_URL/todos/clear_completed)
  if echo "$response" | grep -q '"status":"ok"'; then
    printInfo "✅ Clear completed executed successfully"
  else
    printInfo "❌ Failed to execute Clear completed"
  fi

}



assertBugsAndRedeployment(){
  printInfoSection "Verifying if the Bug 1 is there..."

  _check_bug1
  
  if is_bug1_solved; then
    exit 1
  fi

  printInfo "Bugs are there! now solving 1, 2 and 3 with the TodoController from solution/bug3"
  
  replaceTodoController "solution/bug3"

  redeployApp

  # Wait for the rolled-out app to be reachable via ingress (was waitAppCanHandleRequests on 30100).
  assertRunningApp todoapp

  if ! is_bug1_solved; then
    exit 1
  fi

  if ! is_bug2_solved; then
    exit 1
  fi

  if ! is_bug3_solved; then
    exit 1
  fi

}

replaceTodoController(){

  solution_branch="$1"

  printInfoSection "Replacing the TodoController from branch $solution_branch"

  if [ -z "$solution_branch" ]; then
    printError "No solution branch specified"
    return 1
  fi

  target="app/src/main/java/com/dynatrace/todoapp/TodoController.java"
  url="https://raw.githubusercontent.com/dynatrace-wwse/enablement-live-debugger-bug-hunting/refs/heads/$solution_branch/app/src/main/java/com/dynatrace/todoapp/TodoController.java"
  tmp="${target}.download"

  printInfo "Downloading TodoController.java from $solution_branch branch"

  # --fail makes curl exit non-zero on HTTP 4xx/5xx (e.g. 429 rate-limit) so we
  # never overwrite the source with a "429: Too Many Requests" body. Retries
  # with backoff handle transient rate-limits from raw.githubusercontent.com.
  if ! curl -sSL --fail \
      --retry 5 --retry-delay 5 --retry-max-time 120 \
      --retry-all-errors \
      -o "$tmp" \
      "$url"; then
    printError "❌ Failed to download TodoController.java from $solution_branch (HTTP error or network failure)"
    rm -f "$tmp"
    return 1
  fi

  # Sanity-check the payload: it must look like Java source, not an error body.
  if ! grep -q '^package com.dynatrace.todoapp' "$tmp"; then
    printError "❌ Downloaded TodoController.java does not look like valid Java source. First line: $(head -n1 "$tmp")"
    rm -f "$tmp"
    return 1
  fi

  mv "$tmp" "$target"
  printInfo "✅ TodoController.java downloaded successfully from $solution_branch"

}

assertBug2isThere(){
  printInfoSection "Verifying if the Bug 2 is there..."
  
  is_bug2_solved
  
  if ! is_bug1_solved; then
    exit 1
  fi

}