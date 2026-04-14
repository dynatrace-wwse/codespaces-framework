--8<-- "snippets/prerequisites.js"

# Prerequisites: Quick Step by Step Guide

!!! tip "Prerequisites"
    - Provision Infrastructure
    - Download Visual Studio Code

## Prerequisites
### 1.- Provision Infrastructure 

For the remote environment we'll use an EC2 Instance in the AWS cloud.

Navigate to your AWS Account and open create EC2 instance:

- Give it a name like `Sergio Hinojosa's Environment`
- Select Ubuntu as OS
- Amazon Machine Image (AMI)
    - Ubuntu Server 24.04 LTS (HVM), SSD Volume - Architecture 64-bit (x86)
- Instance Type
    - t3.xlarge (4vCPU 16 GiB Memory)
- Key pair
    - If you don't have one, create it
    - Enter key pair name
    - Type: RSA
    - Format: pem
    - Create the Identity file and download it to your computer (A good place could be something like `/Users/firstname.lastname/.aws/keys/onboarding.pem` )
- Disk
    - Allocate 40 Gig of Disk space, this should be more than enough for your onboarding journey
- Network policies Incoming 22, 80 
    - Optional: Ports 8000, 30100, 30200, 30300 if you want to deploy more apps and want to access them via the default exposure
- Launch instance

??? info "App exposure on http 80"
    The apps in this framework are exposed via Kubernetes NodePort in the ports 30100, 30200 and 30300. For convenience reasons, in this training port 30100 will be routed via IPTABLES to port 80 with the function `exposeOnHttp`. Reason is that some corporate firewalls block traffic in those ports.
<!-- 
t2.xlarge in Virginia Linux base 0.1856 USD
t3.xlarge in Virginia Linux base 0.1664 USD
t2.xlarge in London Linux base 0.2112 USD
t3.xlarge in London Linux base 0.1888 USD
t3.2xlarge in London Linux base 0.3776 USD


--- x.large comparison ---
		virginia	london
	t2/h	0,19 €	0,21 €
	t3/h	0,17 €	0,19 €
24	t2/day	4,45 €	5,07 €
24	t3/day	3,99 €	4,53 €
30	t2/month	133,6320	152,06 €
30	t3/month	119,8080	135,94 €

   t3.2x/month  240 USD a month

		11,54%	11,86%

t2 and t3 increase of 12% increase regardless of zone
---- ----- ----- -----
Performance and CPU Credits:

T2 Instances: Use a fixed CPU credit system. They accumulate CPU credits when idle and spend them when they are active. They have limited baseline CPU performance.
T3 Instances: Are more efficient with a burstable CPU model and are not only capable of sustaining burst performance but can also use unlimited mode, which allows them to exceed their CPU credits whe

In summary, T3 instances provide better overall performance, efficiency, and cost-effectiveness compared to T2 instances. For new applications and workloads, T3 is generally recommended over T2.

-->


### 2.- Download Visual Studio Code

- Go to  [https://code.visualstudio.com](https://code.visualstudio.com), download and install Visual Studio Code on your machine. 

!!! tip "Tip"
    Working on a local Visual Studio Code maximizes your productivity. You'll be able to connect to dev containers remotely or locally, install plugins, and much more.

#### 2.1 - Install VS Code Remote Explorer Extension

- You'll need the Remote Explorer extension `ms-vscode.remote-explorer` so you can connect to your server via SSH and develop from the IDE as if you were working on your localhost.

    ![remote explorer](img/remote-explorer.png){ width="300"; }


### 3.- Dynatrace SaaS Tenant

 - You'll need a **Grail enabled Dynatrace SaaS Tenant** ([sign up here](https://dt-url.net/trial){target="_blank"}) if you don't already have one.

#### 3.1- Enable OneAgent features

Go to Settings > Collect and Capture > General Monitoring settings > OneAgent features

![alt text](img/oneagent_features.png)


##### 3.1.1 - Enable W3C context 

In OneAgent features we enable the [W3C context](https://www.dynatrace.com/knowledge-base/w3c-trace-context/) for the distributed tracing. 

Click save and close

![alt text](img/w3c_context.png)


##### 3.1.2 - Enable Opentelemetry
In OneAgent features we enable the Opentelemetry features.
![alt text](img/opentelemetry_oa.png)

Click save and close

##### 3.1.3 - Enable gRPC features

In OneAgent features we enable the gRPC features.

![alt text](img/grpc_oa.png)

Click save and close

##### 3.1.4 - Enable log enrichment

In OneAgent features we enable log enrichment

![alt text](img/log_enrichment.png)

Click save and close

#### 3.2 - Monitored Technologies

Go to Settings > Collect and Capture > General Monitoring settings > Monitored technologies
![alt text](img/monitored_technologies.png)

##### 3.2.1 - Enable Static Go

Go > Enable Go Static application monitoring

Click save and close

##### 3.2.2 - Enable Envoy

Enable Monitor Envoy

Click save and close
##### 3.2.3 - Enable Python

Enable Monitor Python


Click save and close
#### 3.3 - Built-In Monitoring Rules

Go to Settings > Process and contextualize > Process Groups > Built-in monitoring rules 

##### 3.3.1 - Enable Monitoring Static Go

Disable "Do not monitor processes if Go Binary Linkage equals 'static' - Rule id: 47

Click save and close
![alt text](img/disable_go_rule.png)

Disabling this rule enables deep-monitoring into the checkout service and the product-catlog from the Astroshop.


<div class="grid cards" markdown>
- [Let's launch and configure the remote environment:octicons-arrow-right-24:](configure.md)
</div>
