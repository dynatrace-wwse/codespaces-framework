--8<-- "snippets/configure.js"

# SSH & Host setup

## 1. SSH Connection

### 1.1 Connect via SSH on the Terminal
A public IP has been assigned to your server, let's connect to it via SSH. Let's say I received the public IP `18.171.190.13` and I saved my PEM file in the location `/Users/luke.skywalker/.aws/keys/remote-environment.pem`.

The SSH command for logging in looks like this `ssh -i {identity-file-location} {user}@{ip}`

For this example is:
```bash
ssh -i /Users/luke.skywalker/.aws/keys/remote-environment.pem ubuntu@18.171.190.13
```
since we don't want to type this every time, let's configure the SSH Connection.

### 1.2 Configure SSH Connection

![remoteexplorer](img/remoteexplorer.png){ align=center ; width="450"; }

- Open Visual Studio Code
- On the left panel you'll see a `Remote Explorer` icon, click on it.
- Select `Remotes (Tunnels/SSH)`
- Click on the Wheel ⚙️ icon
- A prompt will appear, which configuration file you want to update, I select `/Users/luke.skywalker/.ssh/config`
- The file will open in VS Code, add the following entry for the SSH client

    ```config title="Users/luke.skywalker/.ssh/config"
    # My Remote Environment
    Host onboarding
    HostName 18.171.190.131
    User ubuntu
    IdentityFile /Users/luke.skywalker/.aws/keys/remote-environment.pem
    ```
    For ease of use the server name will be called `onboarding`. This name will resolve only locally. We assign an IP address to that name, a username and the identity file.

- Save the file and test the connection.

### 1.3 Test SSH Connection

In a terminal type:

```bash
ssh onboarding
```
If you configured correctly, you'll be able to connect to the server successfully. You can also do `ssh onboarding -v` to debug what the SSH Client is doing and from which files it is getting the configuration to connect to that server.

### 1.4 Connect using VS Code

![alt text](img/vscodessh.png){ align=right ; width="300"; }

On the panel now you'll see a server called `onboarding`, if you click on the arrow it will use the instance of VS Code to connect to it, if you click in the + sign, it will create a new VS Code instance and connect to it. 

It will look something like this:

![alt text](img/trustserver.png) 

Trust the author and the contents of the server - after all it's your own playground ;) 

Next, on the left handside, in the Explorer, click on `Open Folder`, select your Home directory, which is `/home/ubuntu/`.

Now you have within VS Code full access to your remote environment! This will boost your onboarding learning experience.


## 2. Prepare Host

We are connecting to a new LTS Ubuntu server, let's install the tools to run the enablement environment.

### 2.1 Clone the repository

Once you shell into the host, open a new terminal and clone the repository.

```bash
git clone https://github.com/dynatrace-wwse/remote-environment
```

This will clone the repo under `/home/ubuntu/remote-environment`

### 2.2 Install DevTools
```bash
cd remote-environment
source .devcontainer/util/source_framework.sh && checkHost
```
![Check Host](img/checkhost.png)

Type `y` to install all requirements for the framework.


### 2.3 Give your Host a friendly hostname (optional)
Since we need to reboot the OS to make the changes effective, specially the access to Docker, let's also give the hostname a friendly name to our server, this name will reflect itself later in Dynatrace when we monitor the infrastructure.  

```bash
sudo hostnamectl set-hostname onboarding
```

### 2.4 Reboot Host

```bash
sudo reboot
```
This command will reboot the server. Make sure you have saved your work since it'll close the SSH connection and you might lose unsaved work on the server. While the server reboot, let's fetch the Kubernetes Monitoring configuration from your Dynatrace Tenant.


??? Info "Public IP of your AWS instance does not change with a reboot"
    Rebooting an EC2 instance will allocate the same public IP as before, only if you stop it and start it again, then AWS will fetch a new public IP for your server and you'll have to reconfigure your SSH connection.

### 2.5 Get Dynakube and Tokens 

While the server is being rebooted (is very quick actually just 2 to 3 minutes) let's fetch the Dynakube and Tokens that we'll use later. 

Go to the Kubernetes App in your Dynatrace environment, on the right hand side click on `+ Add Cluster`

Select:

1. Other distributions
- Kubernetes platform monitoring + Full-Stack observability
- Enable Log management and analytics
- Enable Extensions
- Enable Telemetry endpoints for data ingest remote
- Give the cluster a friendly name  `onboarding` or `remote-environment`
- For Networkzone and Hostgroup give also the same name `onboarding`
![Add Kubernetes cluster](img/monitork8s.png)
- Generate a Dynatrace Operator token and a Data Ingest token 
!!! important "Copy & save the tokens to your clipboard 📋"
	Save the Operator Token and Data Ingest Token to your clipboard
![Dynakube Tokens](img/dynakube_tokens.png)
- 💾 Download the `Dynakube.yaml`file

### 2.6 Set the environment variables

**Set up secrets and environment variables**

Connect back to the Host using VS Code.

!!! info "Open Folder `remote-environment`"
    When you connect back to the host, is important that you open the Folder `remote-directory`, this way VS Code will understand the configuration that is inside `.vscode` and you'll be able to shell into the container easily.

**Create .env file for the secrets**

Inside the `remote-environment` create an .env file in `.devcontainer/.env`

!!! info "Sample `.env` file"
	You can copy and paste the following sample into `.devcontainer/.env`. Your environment file should look similar to this:

	```properties title=".devcontainer/.env" linenums="1"
	# Environment variables as defined as secrets in the devcontainer.json file
	# Dynatrace Tenant
	DT_ENVIRONMENT=https://abc123.sprint.apps.dynatracelabs.com
		
    # Dynatrace Operator Token
	DT_OPERATOR_TOKEN=dt0c01.XXXXXX

	# Dynatrace Ingest Token
	DT_INGEST_TOKEN=dt0c01.YYYYYY

	```
    
    ??? tip "In VS Code create new .env file"
        Go to remote-environment > .devcontainer > runlocal > new File (.env), paste the contents of the sample .env file and reflect your tenant and tokens.
        ![alt text](img/envfile.png)



<div class="grid cards" markdown>
- [Let's launch the environment:octicons-arrow-right-24:](launch.md)
</div>
