#!/bin/bash
# This file contains the functions synchronizing multiple repos and their files, specially the important function files.

source .devcontainer/util/source_framework.sh

ROOT_PATH="$(dirname "$PWD")/"
SYNCH_REPO=$RepositoryName

printInfo "Using as ROOT_PATH: $ROOT_PATH"
printInfo "This is synchronization repo: $SYNCH_REPO"


this_repos=("codespaces-framework")
all_repos=("codespaces-framework" "enablement-codespaces-template" "enablement-live-debugger-bug-hunting" "enablement-gen-ai-llm-observability" "enablement-business-observability" "enablement-dql-301" "enablement-dynatrace-log-ingest-101" "enablement-kubernetes-opentelemetry" "enablement-browser-dem-biz-observability" "enablement-workflow-essentials" "workshop-dynatrace-log-analytics" "demo-bug-busters" )
synch_repos=("enablement-codespaces-template" "enablement-live-debugger-bug-hunting" "enablement-gen-ai-llm-observability" "enablement-business-observability" "enablement-dql-301" "enablement-dynatrace-log-ingest-101" "enablement-kubernetes-opentelemetry" "enablement-browser-dem-biz-observability" "enablement-workflow-essentials" "workshop-dynatrace-log-analytics" "demo-bug-busters" )
synch2_repos=("enablement-codespaces-template" "enablement-live-debugger-bug-hunting" "enablement-gen-ai-llm-observability" "enablement-business-observability" "enablement-dql-301" "enablement-dynatrace-log-ingest-101" "enablement-kubernetes-opentelemetry" "enablement-browser-dem-biz-observability" "enablement-workflow-essentials" "demo-bug-busters" )
cs_repos=("enablement-codespaces-template" "enablement-live-debugger-bug-hunting" "enablement-gen-ai-llm-observability" "enablement-business-observability" "enablement-dynatrace-log-ingest-101" "enablement-browser-dem-biz-observability")
fix_repos=("demo-bug-busters")
migrate_repos=("enablement-dql-301" "enablement-workflow-essentials" "enablement-kubernetes-opentelemetry")
refactor_repos=("codespaces-framework" "enablement-codespaces-template" "workshop-dynatrace-log-analytics")
import_repos=("workshop-dynatrace-log-analytics")
logs_repos=("workshop-dynatrace-log-analytics")
unguard_repos=("demo-mcp-unguard")


# Function to compare files in arrays,
# $1[cs or all for iterating in only CS or ALL repos]
# $2=filepath
compareFile() {
    local array_name="$1_repos"
    eval "local array=(\"\${$array_name[@]}\")"
    printInfoSection "Comparing $2 from $ROOT_PATH$SYNCH_REPO"
    for repo in "${array[@]}"; do

        diff "$ROOT_PATH$SYNCH_REPO/$2" "$ROOT_PATH$repo/$2" >/dev/null
        if [ $? -eq 0 ]; then
            printInfo "$repo - $2 =="
        else
            printInfo "$repo - $2 !="
            code --diff "$ROOT_PATH$SYNCH_REPO/$2" "$ROOT_PATH$repo/$2" >/dev/null
        fi
    done
}


# Function to copy all files from one repository to another. A branch will be created and 
# the diff will show the difference between files so no functionality gets lost.
copyFramework(){
    repo=$(basename $(pwd))

    printInfoSection "Copying core files to repository $repo into actual branch"
    printInfoSection "Copying core files from $ROOT_PATH$SYNCH_REPO to $ROOT_PATH$repo"
    
    # Exclude core files from the synchronizer
    EXCLUDES=(--exclude='.git' --exclude='synchronizer/' --exclude='/README.md')

    if [ "$EXCLUDE_MKDOC" = true ]; then
        printInfo "excluding mkdoc"
        EXCLUDES+=(--exclude='docs/' --exclude='mkdocs.yaml')
    fi
    if [ "$EXCLUDE_CUSTOMFILES" = true ]; then
        printInfo "excluding custom files"
        EXCLUDES+=(
    --exclude='.devcontainer/devcontainer.json'
    --exclude='.devcontainer/post-*.sh'
    --exclude='.devcontainer/util/my_functions.sh'
    --exclude='.devcontainer/test/integration.sh'
        )
    fi

    # For copying
    if [ "$IMPORT" = true ]; then
        # For importing changes we invert
        DEST="$ROOT_PATH$SYNCH_REPO/"
        SOURCE="$ROOT_PATH$repo/"
    else
        SOURCE="$ROOT_PATH$SYNCH_REPO/"
        DEST="$ROOT_PATH$repo/"
    fi

    rsync -av "${EXCLUDES[@]}" "$SOURCE" "$DEST"

}

# Function to compare files in arrays,
# $1[cs or all for iterating in only CS or ALL repos]
# $2=filepath
copyFile() {
    local array_name="$1_repos"
    eval "local array=(\"\${$array_name[@]}\")"
    printInfoSection "Comparing $2 with ${array[@]}"
    for repo in "${array[@]}"; do
        printInfo "copy $2 in repo $repo "
        cp "$ROOT_PATH$SYNCH_REPO/$2" "$ROOT_PATH$repo/$2"
    done
}

cherryPick() {
    local array_name="$1_repos"
    eval "local array=(\"\${$array_name[@]}\")"
    printInfoSection "Merging commit $2 from $ROOT_PATH$SYNCH_REPO"
    for repo in "${array[@]}"; do
        cd $ROOT_PATH"$repo" >/dev/null
        #git remote add synchronizer https://github.com/dynatrace-wwse/codespaces-framework
        git fetch synchronizer
        #git checkout -b synch
        git cherry-pick $2
        if [ $? -eq 0 ]; then
            printInfo "$repo - $2 ->CherryPick OK"
        else
            printInfo "$repo - $2 ->CherryPick NOK"
        fi

        cd - >/dev/null
    done
}

helperFunction() {
    local array_name="$1_repos"
    eval "local array=(\"\${$array_name[@]}\")"
    printInfoSection "Doing something in repo from this as base $ROOT_PATH$SYNCH_REPO"
    for repo in "${array[@]}"; do
        printInfo "in repo $repo "
        cd $ROOT_PATH"$repo" >/dev/null
        
        ## Resetting
        #git reset --hard HEAD
        #git clean -f
        #git branch -D $BRANCH

        ## Cleaning for main
        #git checkout main
        #git pull origin main
        #git checkout -b monitoring/main
        #git status
        #
        ##Add and commit
        git add .
        git commit -s -m "$BODY"
        git push origin $BRANCH

        
        cd - >/dev/null
    done
}

doInRepos() {
    local array_name="$1_repos"
    eval "local array=(\"\${$array_name[@]}\")"
    # Pass the function name as an argument
    func_name=$2
    #printInfoSection "Iterating in array ($1) with function($2) with argument $3 tied to $ROOT_PATH$SYNCH_REPO"
    
    for repo in "${array[@]}"; do
        #printInfo "for $repo do $2  - $@ "
        # go to repo
        cd $ROOT_PATH"$repo" >/dev/null

        # Call the function using indirect expansion
        $func_name $3 $4 $5 $6 $7 $8

        # go back to previous directory
        cd - >/dev/null
    done
}


cherryPickMerge() {
    
    repo=$(basename $(pwd))

    printInfoSection "Doing CherryPick Merge for $repo with Cherry: $CHERRYPICK_ID"
    printInfo "PR '$TITLE'. '$BODY'"

    # go to main and create branch from there
    git checkout main

    # get latest changes
    git pull 

    # Add synchronizer as remote repo
    git remote | grep -q '^synchronizer$'
    if [ $? -eq 0 ]; then
        # Synchronizer exists, pruning it to avoid conflicts
        git remote prune synchronizer
    else
        git remote add synchronizer https://github.com/dynatrace-wwse/codespaces-framework
    fi
    # fetch synchronizer
    git fetch synchronizer

    # create branch with merge id
    git checkout -b "synch/$CHERRYPICK_ID"

    git cherry-pick -m 1 $CHERRYPICK_ID
    if [ $? -eq 0 ]; then
        printInfo "$repo - $CHERRYPICK_ID ->CherryPick OK"

        doPushandPR
    else
        printInfo "$repo - $1 ->CherryPick NOK"
    fi

}



listOpenIssues(){
    repo=$(basename $(pwd))
    issue=$(gh issue list --state open --json number,title,url \
      --jq '.[] | "\(.number) | \(.title) | \(.url)"')
    
    printInfo "$issue"
}


doPushandPR(){
    repo=$(basename $(pwd))

    printInfo "Pushing $BRANCH for $repo"
    git push origin $BRANCH
    
    printInfo "creating PR for dynatrace-wwse/$repo"

    gh repo set-default dynatrace-wwse/$repo
    
    gh pr create --base main --head $BRANCH --title "$TITLE" --body "$BODY"

    printWarn "A PR will be created with an URL, next step is to Merge the PR automatically if the check is ok."
}

verifyPrMerge(){
    repo=$(basename $(pwd))
    printInfoSection "verifying PR for branch $BRANCH, merging and deleting branch if ok for repo $repo"

    PR=$(GH_PAGER=cat gh pr list | grep $BRANCH)
    if [[ -z "$PR" ]]; then
        printWarn "No PR found for branch $BRANCH"
        #PRS=$(GH_PAGER=cat gh pr list)
        #printInfo "Other PR (if any)... \n$PRS"
    else 
        
        printInfo "$PR"

        PR_ID=$(echo $PR | awk '{print $1}') 
        CHECKS_PASS=$(gh pr checks $PR_ID --json state | jq 'all(.[]; .state == "SUCCESS")')
        printInfo "Checks: $CHECKS_PASS"
        if [[ $CHECKS_PASS == true ]]; then
            printInfo " ✅ All checks have passed: $CHECKS_PASS"
            gh pr merge $PR_ID --merge --delete-branch
        else
            printError "❌ Checks failed $CHECKS_PASS"
        fi
    fi
}

tagAndCreateRelease(){
    repo=$(basename $(pwd))
    printInfoSection "Creating a tag $TAG and release $RELEASE for repo $repo"
    git checkout main
    git pull origin main
    git tag -a $TAG -m "Release version $RELEASE"
    git push origin $TAG
    gh repo set-default dynatrace-wwse/$repo
    gh release create $TAG -t "$RELEASE" -n "$BODY"
}

mergePr(){
    repo=$(basename $(pwd))
    printInfo "Merging synch/$CHERRYPICK_ID for $repo"
    git pull origin main
    git checkout main
    git merge synch/$CHERRYPICK_ID
    git push -u origin main
}

protectMainBranch(){
    repo=$(basename $(pwd))
    printInfo "Protecting branch main for $repo"
    
result=$(gh api --method PUT /repos/dynatrace-wwse/$repo/branches/main/protection \
  --input - <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "codespaces-integration-test-with-dynatrace-deployment"
    ]
  },
  "enforce_admins": true,
  "allow_deletions": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
EOF
)
echo "Status:$?"
echo $result
}


deleteBranches(){
    repo=$(basename $(pwd))
    printInfo "Deleting unmerged branches $repo"

    #List merged branches
    printInfo "Merged Branches \n $(git branch --merged)"

    # delete merged branches
    git branch --merged | grep -vE '^\*|main|master' | xargs -n 1 git branch -d

    # Fetch latest remote info
    printInfo "Latest remote info \n$(git fetch -p)"

    # List merged branches
    printInfo "Merged Branches remote \n $(git branch -r --merged origin/main)"

    # Delete Remote Branches
    git branch -r --merged origin/main | grep -vE 'origin/main|origin/master' | sed 's/origin\///' | xargs -n 1 -I {} git push origin --delete {}

}


generateMarkdowntable(){
    repo=$(basename $(pwd))
    echo "[$repo](https://github.com/dynatrace-wwse/$repo) |  description $repo | [![Integration tests](https://github.com/dynatrace-wwse/$repo/actions/workflows/integration-tests.yaml/badge.svg)](https://github.com/dynatrace-wwse/$repo/actions) [![Version](https://img.shields.io/github/v/release/dynatrace-wwse/$repo?color=blueviolet)](https://github.com/dynatrace-wwse/$repo/releases)  [![Commits](https://img.shields.io/github/commits-since/dynatrace-wwse/$repo/latest?color=ff69b4&include_prereleases)](https://github.com/dynatrace-wwse/$repo/graphs/commit-activity) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?color=green)](https://github.com/dynatrace-wwse/$repo/blob/main/LICENSE) [![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-brightgreen)](https://dynatrace-wwse.github.io/$repo/) " 
}
