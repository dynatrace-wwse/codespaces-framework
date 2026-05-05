#!/bin/bash
# Build script for building locally and launching the training environment in a Docker container without Visual Studio Code.
# Tested on Ubuntu 22.04 and 24.04 LTS with Docker installed.

# Resolve paths relative to this script (works from cache or local)
_MAKEFILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${_MAKEFILE_DIR}/runlocal/helper.sh"
ENV_FILE="${ENV_FILE:-${_MAKEFILE_DIR}/.env}"

NAMESPACE="shinojosa"
IMAGENAME="dt-enablement"
REPOSITORY=$NAMESPACE/$IMAGENAME
TAG="v1.2"

REPOTAG=$REPOSITORY:$TAG

# Calculates the RepositoryName from the base path needed for setting the directory so the framework can load inside the container.
getRepositoryName

# Loads variables k=v from the .env file into DOCKER_ENVS such as DT_ENVIRONMENT, so they can be added as environment variables to the Docker container.
getDockerEnvsFromEnvFile

# Commands to be executed in the container after it is created (as in VSCode devcontainer.json)
CMD="./.devcontainer/post-create.sh; ./.devcontainer/post-start.sh; zsh"

# Ports to map to the host, add as many as wanted
# Port 80/443 for ingress, 30100-30300 for legacy NodePort, 8000 for mkdocs
PORTS="-p 80:80 -p 443:443 -p 30100:30100 -p 30200:30200 -p 30300:30300 -p 8000:8000"

VOLUMEMOUNTS="-v /var/run/docker.sock:/var/run/docker.sock -v /lib/modules:/lib/modules -v $(dirname "$PWD"):/workspaces/$RepositoryName"

WORKINGDIR="-w /workspaces/$RepositoryName"

buildNoCache(){
    # Build the image with no cache
    docker build --no-cache -t $REPOTAG .
    echo "Building completed."   
}


buildx(){
    docker buildx build --no-cache --platform linux/amd64,linux/arm64 -t $REPOTAG --push .
}

build(){
    # Build the image
    echo "Building the image $REPOTAG ..."
    docker build -t $REPOTAG .
    echo "Building completed."
}


run(){
    if [ -z "$1" ]; then
        echo "starting docker container with: $CMD"
    else
        echo "running in container: $1"
        CMD="$1"
    fi

    docker run $DOCKER_ENVS \
        --name $IMAGENAME \
        --privileged \
        --dns=8.8.8.8 \
        --network=host \
        $PORTS \
        $VOLUMEMOUNTS \
        $WORKINGDIR \
        -it $REPOTAG \
        /usr/bin/zsh -c "$CMD"
}

start(){
    status=$(docker inspect -f '{{.State.Status}}' "$IMAGENAME")
    if [ "$status" = "exited" ] || [ "$status" = "dead" ]; then
        echo "Container is stopped removing container."
        # Add repository name to the environment variables for the container
        docker rm $IMAGENAME
        echo "Starting a new container"
        run 
    elif  [ "$status" = "running" ]; then 
        echo "Container $IMAGENAME is running, attaching new shell to it"
        docker exec -it $IMAGENAME zsh 
    else
        echo "Image $IMAGENAME is not found."
        if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^$IMAGENAME$"; then
            echo "Image exists locally, running it."
        else
            echo "Image does not exist locally. Building it first, if you want to build your own, do 'make build'"
        fi
        run
    fi
}

cleanStart(){
    echo "Cleaning up all containers for a fresh start..."

    # 1. Stop and remove the framework container
    echo "Removing framework container '$IMAGENAME'..."
    docker kill "$IMAGENAME" 2>/dev/null
    docker rm -f "$IMAGENAME" 2>/dev/null

    # 2. Kill and remove K3s container
    echo "Removing K3s containers..."
    docker rm -f k3s-enablement 2>/dev/null

    # 3. Kill and remove Kind containers (kind-control-plane, *-control-plane)
    echo "Removing Kind containers..."
    kind_containers=$(docker ps -a --filter "name=control-plane" -q 2>/dev/null)
    if [ -n "$kind_containers" ]; then
        docker rm -f $kind_containers 2>/dev/null
    fi

    # 4. Delete Kind clusters if kind CLI is available on host
    if command -v kind &>/dev/null; then
        for cluster in $(kind get clusters 2>/dev/null); do
            echo "  Deleting Kind cluster: $cluster"
            kind delete cluster --name "$cluster" 2>/dev/null
        done
    fi

    # 4. Remove any stale/exited containers from previous runs
    stale=$(docker ps -a --filter "status=exited" --filter "status=dead" -q 2>/dev/null)
    if [ -n "$stale" ]; then
        echo "Removing stale containers..."
        docker rm -f $stale 2>/dev/null
    fi

    echo "All containers cleaned. Starting a fresh environment..."
    run
}

cleanCache(){
    echo "Cleaning framework cache..."
    local container_cache="${_MAKEFILE_DIR}/.cache"
    local host_cache="${HOME}/.cache/dt-framework"

    if [ -d "$container_cache" ]; then
        rm -rf "$container_cache"
        echo "Container cache deleted: $container_cache"
    fi

    if [ -d "$host_cache" ]; then
        rm -rf "$host_cache"
        echo "Host cache deleted: $host_cache"
    fi

    echo "Cache cleaned."
}

integration(){
    echo "Executing Integration-Tests -"
    CMD_TEST=" ./.devcontainer/test/integration.sh"
    CMD+="$CMD_TEST"
    
    status=$(docker inspect -f '{{.State.Status}}' "$IMAGENAME")
    if [ "$status" = "exited" ] || [ "$status" = "dead" ]; then
        echo "Container is stopped removing container."
        # Add repository name to the environment variables for the container
        docker rm $IMAGENAME
        echo "Starting a new container"
        run 
    elif  [ "$status" = "running" ]; then 
        # FIXME: Better to load test functions in framework or call the script from a function in the framework in an extra shell. 
        echo "Container $IMAGENAME is running, running the tests inside the running container (WIP)..."
        docker exec -t $IMAGENAME "zsh"
    else
        echo "Image $IMAGENAME is not found."
        if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^$IMAGENAME$"; then
            echo "Image exists locally, running it."
        else
            echo "Image does not exist locally. Building it first, if you want to build your own, do 'make build'"
        fi
        run
    fi
}