

Adding a submodule for the demo.live astroshop


git submodule add https://github.com/Dynatrace/opentelemetry-demo-gitops.git opentelemetry-demo-gitops

git submodule init


cd opentelemetry-demo-gitops


git fetch --tags


git checkout <commit-hash>



cd ..
git add opentelemetry-demo-gitops


git commit -m "Add Dynatrace opentelemetry-demo-gitops submodule at version v0.1.0"



#!/bin/bash
# Script to add Dynatrace opentelemetry-demo-gitops as a submodule and lock to a specific version

REPO_URL="https://github.com/Dynatrace/opentelemetry-demo-gitops.git"
SUBMODULE_PATH="opentelemetry-demo-gitops"
VERSION="v0.1.0"  # Change this to your desired tag or commit

echo "Adding submodule..."
git submodule add $REPO_URL $SUBMODULE_PATH

echo "Initialising and updating submodule..."
git submodule init
git submodule update

echo "Checking out version $VERSION..."
cd $SUBMODULE_PATH
git fetch --tags
git checkout $VERSION
cd ..

echo "Staging and committing changes..."
git add $SUBMODULE_PATH
git commit -m "Add Dynatrace opentelemetry-demo-gitops submodule at version $VERSION"

echo "Done! Push your changes with: git push"