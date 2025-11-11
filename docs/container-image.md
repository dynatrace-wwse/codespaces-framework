--8<-- "snippets/container-image.js"

## Overview


The Dynatrace Enablement Framework leverages a custom Docker image as the foundation for all training and demo environments. This image is engineered for maximum compatibility, flexibility, and ease of use across a variety of platforms and deployment scenarios.

---

## Key Features

### 🖼️ Base Image:
  The framework uses `mcr.microsoft.com/devcontainers/base:ubuntu` as its base image, ensuring seamless compatibility with GitHub Codespaces and Visual Studio Code Dev Containers.


### 💻 Cross-Platform Support:
  The image is built to run on both AMD and ARM architectures, eliminating vendor lock-in and supporting a wide range of hardware.


### ☁️ Local and Cloud Execution:
  - Supports GitHub Codespaces for cloud-based development.
  - Enables local execution on Windows, Linux, and macOS via Multipass, ensuring a consistent development environment regardless of host OS.


## <img src="https://cdn.bfldr.com/B686QPH3/at/w5hnjzb32k5wcrcxnwcx4ckg/Dynatrace_signet_RGB_HTML.svg?auto=webp&format=pngg" alt="DT logo" width="22"> Dynatrace Integration
Dynatrace OneAgent FullStack and Kubernetes CloudNativeFullstack deployments are fully supported. All required components—including the CSI Driver, Webhook, ActiveGate, and OneAgents—can be deployed within this image, ensuring comprehensive monitoring and observability for running applications.

## Tooling

!!! tip "🛠️ Included tooling "
    The image comes with a comprehensive set of tools required for modern DevOps and cloud-native development, including:

    - [Helm](https://helm.sh)
    - [Kubectl](https://kubernetes.io/docs/reference/kubectl/)
    - [Kind](https://kind.sigs.k8s.io/)
    - [Docker](https://www.docker.com/)
    - [NodeJs](https://nodejs.org/)
    - [K9s](https://k9scli.io/)
    - [Python](https://www.python.org/)




## Docker-in-Socket Strategy

The Dynatrace Enablement Framework uses a **Docker-in-socket** strategy to enable container management from within the development container. By mounting the Docker socket (`/var/run/docker.sock`) from the host into the container, the development environment can communicate directly with the host's Docker daemon.


### How It Works

- The `entrypoint.sh` script inside the container manages interactions with the Docker daemon.
- By sharing the Docker socket, the container can execute Docker commands as if running directly on the host.
- This enables workflows such as building, running, and managing additional containers from within your Codespace or Dev Container.


### Benefits

- **Consistency:** Docker commands behave identically inside the container and on the host.
- **Flexibility:** Supports advanced scenarios such as running nested containers or orchestrating multi-container setups.
- **Simplicity:** No need to install Docker inside the container; it leverages the host’s Docker installation.


### Example

In the `devcontainer.json`, mount the Docker socket as follows:

```json
"mounts": ["source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"]
```

## Special Container Runtime Arguments

The following `runArgs` configuration in `devcontainer.json` enhances the capabilities of the development container:

```json
"runArgs": ["--init", "--privileged", "--network=host"]
```

- **--init:** Runs an init process inside the container to handle zombie processes and signal forwarding, improving container stability.
- **--privileged:** Grants the container extended privileges, allowing access to all host devices and enabling operations typically restricted in standard containers. This is useful for scenarios requiring low-level system access (e.g., running Docker inside Docker or accessing host resources).
- **--network=host:** Shares the host’s networking stack with the container, enabling direct use of the host’s network interfaces. This is helpful for networking tests or when services inside the container need to be accessible on the host network.



## Image Distribution
The image is hosted on Docker Hub and is cross-compiled for ARM and AMD architectures.
[![Downloads](https://img.shields.io/docker/pulls/shinojosa/dt-enablement?logo=docker)](https://hub.docker.com/r/shinojosa/dt-enablement)


## Using the Image in devcontainer.json
You can configure your development container to use the pre-built image or build it yourself from a Dockerfile, depending on your requirements.
### Using the Pre-built Image
To use the pre-built image, specify the "image" property in your devcontainer.json file:

``` json
  // Pulling the image from the Dockerhub, runs on AMD64 and ARM64. Pulling is normally faster.
  "image":"shinojosa/dt-enablement:v1.2",

```
This will pull the published image from Docker Hub and use it as the base for your Codespace or Dev Container.

### Building the Image with VS Code
If you want to build the image yourself (for example, to customise it), you need to use the "build" section in your devcontainer.json. Uncomment or add the following:
``` json
  // "image": "shinojosa/dt-enablement",  
  "build": {    
    "dockerfile": "Dockerfile"  }
    },
```

Comment out or remove the "image" line.
Uncomment or add the "build" section, pointing to your Dockerfile.
This will instruct the environment to build the image locally using your Dockerfile.

## Cross-Compiling with Buildx

In the `.devcontainer` folder, there is a `Makefile` that includes a `buildx` target specifically designed for cross-compiling the container image.

To use cross-compilation:

- Make sure your host architecture is **ARM**.
- Run the `buildx` target from the `Makefile` to build the image for multiple architectures.

Example usage:

```bash title="CrossCompiling target" linenums="0"

make buildx

```

---


<div class="grid cards" markdown>
- [Let's continue:octicons-arrow-right-24:](instantiation-types.md)
</div>
