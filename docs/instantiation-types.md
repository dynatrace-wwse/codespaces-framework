--8<-- "snippets/instantiation-types.js"

!!! success "Choose the option that best fits your needs! 🚀" 
    The Dynatrace Enablement Framework supports multiple ways to instantiate your development environment. You can run it in Github Codespaces, VS Code Dev Containers or local containers, in AMD or ARM architectures.  



## 🏃🏻‍♂️ Quick Step by Step guide: Instantiating Your Environment

### 1. Running in GitHub Codespaces ![run codespace](img/run_codespace.png){ align=right ; width="300"}
1. Go to the repository hosted on GitHub.
2. Click the **<> Code** button.
3. Create a new Codespace using the main branch, or click **+ New** to customize how and where to run the Codespace within GitHub Cloud.

_Repository secrets such as `DT_ENVIRONMENT`, `DT_OPERATOR_TOKEN`, and `DT_INGEST_TOKEN` (among others) are injected automatically using GitHub Codespaces secrets. No manual setup is required—these are available as environment variables inside the container._

### 2.  📦 Running in VS Code Dev Containers or Local Container


??? info "Key Difference: VS Code Dev Container vs Local Container"
	The main difference between a VS Code Dev Container and a local container is how each environment is created and managed. A VS Code Dev Container is launched and orchestrated by VS Code using the configuration in `devcontainer.json`. A local container is started independently using the Makefile and `runlocal` script, allowing you to build and manage the container from the terminal without relying on VS Code. This is ideal for headless or automated workflows.


The following steps apply to both scenarios:

1. **Provision the infrastructure**
    
	??? info "🏗️ Setting up the Infrastructure"
		You may provision your infrastructure on any major cloud provider or run locally using [Multipass](#using-multipass-for-local-development).

		**Minimum requirements for a cloud or local machine:**
		
		1. **Operating System:** Ubuntu LTS (22.04 or 24.04 recommended)
		2. **CPU & Memory:** Requirements depend on your workloads. As a guideline, refer to the `hostRequirements` section in `.devcontainer.json`. A typical setup with 4 CPU cores and 16 GB RAM is sufficient for most use cases.
		3. **Network Ports:** Ensure the following ports are open for inbound connections:
			1. `22` (SSH)
			2. `30100`, `30200`, `30300` (for application access; each deployed app is exposed via Kubernetes NodePort)

2. **SSH into the host**

3. **Clone the repository**

4. **Set up secrets and environment variables**
	- Define all required secrets as environment variables. For both VS Code Dev Containers and local containers, create a `.env` file under `.devcontainer/runlocal/.env`.
	- The secrets required are defined in the `secrets` section of `.devcontainer.json`. If no secrets are needed, create an empty `.env` file.

	??? info "Sample `.env` file"
		You can copy and paste the following sample into `.devcontainer/runlocal/.env`. Ensure all required secrets for the training are included.

		```properties title=".devcontainer/runlocal/.env" linenums="1"
		# Environment variables as defined as secrets in the devcontainer.json file
		# Dynatrace Tenant
		DT_ENVIRONMENT=https://abc123.apps.dynatrace.com

		# Dynatrace Operator Token
		DT_OPERATOR_TOKEN=dt0c01.XXXXXX

		# Dynatrace Ingest Token
		DT_INGEST_TOKEN=dt0c01.YYYYYY

		# Add any other environment variables as needed
		```

5. **Verify prerequisites**
	- Ensure `make` and `docker` are installed on the host and the user has access to Docker.
    
	??? info "Verify prerequisites with `checkHost`"
		Use the provided function to verify requirements. If any are missing, the function offers to install them for you.
		```bash
		source .devcontainer/util/source_framework.sh && checkHost
		```
		![checkhost](img/checkhost.png){ align=center ; width="800";}

!!! success "Ready to Launch"
	You are all set! Launch the enablement with VS Code as a dev container or with `make` as a plain Docker container.


#### 2. a. 📦 🖥️ Running as dev container with VS Code 

1. Let's tell VS Code to read the secrets as environment variables from an `.env`file. Modify the `runArgs` in `.devcontainer/devcontainer.json` and add `"--env-file", ".devcontainer/runlocal/.env"`like the following:
	```json
	"runArgs": ["--init", "--privileged", "--network=host", "--env-file", ".devcontainer/runlocal/.env"]
	```
- This ensures all variables in `.devcontainer/runlocal/.env` are available inside the container.
- ![run codespace](img/vscode_container.png){ align=right ; width="400"}Open the folder in VS Code and use the Dev Containers extension to "Reopen in Container". VS Code will use the `.devcontainer/devcontainer.json` definition to build and start the environment for you.
- You can rebuild the container at any time by typing ```[CTRL] + Shift P > Dev Containers: Rebuild and reopen in container```

#### 2. b. 📦 🐳 Running as local container with make
1. Navigate to `.devcontainer` folder and run:
	```sh
	make start
	```
- This will build and launch the container. All ports, volumes, and environment variables are set up automatically.
	
	!!! info "Protip: create a new Terminal"
		For attaching a new Terminal to the container, just type `make start`.

- Secrets and environment variables are loaded from `.devcontainer/runlocal/.env`. 
- The `makefile.sh` script passes the variables to Docker at runtime such as arguments, volume mounts and port-forwarding. The devcontainer.json file is not used with this set-up.


## Instantiation Types 
### 1. ☁️ GitHub Codespaces

- **One-click cloud dev environments**  
- No local setup required—just click  
- [Learn more about Codespaces](https://github.com/features/codespaces)

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://github.com/codespaces/new?template_repository=dynatrace-wwse/codespaces-framework)

### 2. 🖥️ VS Code Dev Containers

- Use the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) for a seamless local experience in VS Code
- All configuration is in `.devcontainer/devcontainer.json`
- Supports secrets, port forwarding, and post-create hooks


### 3. 🐳 Local Container

- Run the same environment on your machine using Docker.
- Easiest way: just run `make start` in the `.devcontainer` folder.
- This will build and launch the container if needed, or attach to it if already running.
- All ports, volumes, and environment variables are set up for you automatically.



## ⚡ Quick Comparison

| Type                  | Runs On              | VS Code Needed | Fast Start | Customizable | Secrets Handling | Port Forwarding | Best For                  |
|-----------------------|:--------------------:|:--------------:|:----------:|:------------:|:---------------:|:---------------:|---------------------------|
| ☁️ Codespaces         | GitHub Cloud         | ❌             | ✅         | ❌           | Auto-injected   | Auto            | Quick onboarding, demos   |
| 🖥️ VS Code DevContainer | Provided Infrastructure | ✅             | ✅         | ✅           | Auto/manual     | Auto            | Full-featured local dev   |
| 🐳 Local Container    | Provided Infrastructure | ❌             | ✅         | ✅           | Manual/`.env`   | Manual/Makefile | Reproducible local dev    |


## 🔐 Secrets & Environment

Secrets and environment variables are handled differently depending on the instantiation type:

| Instantiation Type         | How Secrets Are Provided                                                                 | Where to Configure/Set                        | Notes                                                                                 |
|---------------------------|----------------------------------------------------------------------------------------|-----------------------------------------------|---------------------------------------------------------------------------------------|
| ☁️ Codespaces             | Auto-injected as environment variables from GitHub Codespaces secrets                   | GitHub repository > Codespaces secrets         | No manual setup; secrets available at container start                                 |
| 🖥️ VS Code Dev Containers | Passed as environment variables via `runArgs` and `.env` file                          | `.devcontainer/devcontainer.json`, `.devcontainer/runlocal/.env`      | Edit/add `.devcontainer/runlocal/.env` for local secrets; `runArgs` must include `--env-file`                    |
| 🐳 Local Container        | Loaded from `.devcontainer/runlocal/.env` file and passed to Docker at runtime by `makefile.sh`                | `.devcontainer/runlocal/.env`, `makefile.sh`   | Run `make start` in `.devcontainer`; secrets loaded at container start                |



<!--TODO How to show logs of container  -->
<!--TODO Add Troubleshooting guide with docker ps -->

---

## 🏠 Running locally

### Using Multipass for Local Development

[Multipass](https://multipass.run/) is a lightweight VM manager from Canonical that makes it easy to launch and manage Ubuntu virtual machines on macOS, Windows, and Linux. This is especially useful if you want to run the framework in a clean, reproducible Ubuntu environment without dual-booting or using a full desktop VM.

**Why use Multipass?**

- Ensures compatibility with Ubuntu-based dev containers and scripts
- Isolates your development environment from your host OS
- Quick to launch, easy to reset or remove

#### Basic usage

  -  **Install Multipass** ([instructions](https://multipass.run/install)) 
  -  **Launch an Ubuntu VM:**
	```sh
	multipass launch --name enablement --disk 30G --cpus 8 --memory 32G
	multipass shell enablement
	```

!!! tip "Mounting Volumes on Multipass"
    You can mount folders from your host into the VM using `multipass mount` if you want to edit code locally but run containers in the VM. For example in the following example we are creating a VM mounting the folder `enablement` where you have all repositories of the enablement framework you want to use. 
    ```bash
    multipass launch --name enablement --disk 30G --cpus 8 --memory 32G --mount  /Users/sergio.hinojosa/repos/enablement:/home/ubuntu/enablement
    ```

<div class="grid cards" markdown>
- [Let's continue:octicons-arrow-right-24:](dynatrace-integration.md)
</div>