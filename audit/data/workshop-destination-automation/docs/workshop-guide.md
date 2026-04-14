# Workshop Guide: Dynatrace + Ansible Automation Platform + AI Workload

## Overview

This workshop demonstrates a production-style workflow using:

- **Red Hat Ansible Automation Platform (AAP)** — automated build, deploy, and lifecycle management
- **Dynatrace** — full-stack observability and AI-powered monitoring
- **AI Travel Advisor App** — a containerized AI workload (FastAPI + inference models)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                 Ansible Automation Platform (Controller + EDA)             │
│                                                                             │
│  Provisioning                 Platform Config            Dynatrace Config   │
│  ┌──────────────────────┐     ┌──────────────────────┐   ┌───────────────┐ │
│  │ install_aap_         │     │ configure_aap.yml    │   │ deploy_       │ │
│  │ containerized.yml    │     │ configure_eda.yml    │   │ dynatrace_*.yml│ │
│  └──────────┬───────────┘     └──────────┬───────────┘   └───────┬───────┘ │
│             │                            │                       │         │
│             ▼                            ▼                       ▼         │
│  ┌──────────────────────┐     ┌──────────────────────┐   ┌───────────────┐ │
│  │ build_custom_ee.yml  │     │ JT/Creds/Inventory   │   │ Monaco +      │ │
│  │ build_custom_de.yml  │     │ Domains + EDA objs   │   │ Settings API  │ │
│  └──────────┬───────────┘     └──────────┬───────────┘   └───────┬───────┘ │
└─────────────┼────────────────────────────┼────────────────────────┼─────────┘
          │                            │                        │
          ▼                            ▼                        ▼
  ┌──────────────────────┐     ┌──────────────────────┐   ┌──────────────────┐
  │ Workshop Host        │     │ AAP Job Templates    │   │ Dynatrace Tenant │
  │ (aap-service-account │     │ workflow-deploy-app  │   │ Apps + Settings  │
  │  rootless Podman)    │     │ + deploy roles       │   │ + OneAgent +     │
  └──────────┬───────────┘     └──────────┬───────────┘   │ EdgeConnect      │
          │                            │               └──────────────────┘
          ▼                            ▼
  ┌──────────────────────┐     ┌──────────────────────┐
  │ easyTravel AI stack  │◄────│ EDA webhook +        │
  │ (public :81)         │     │ automation triggers   │
  │ travel-advisor :8080 │     └──────────────────────┘
  └──────────────────────┘
```

---

## Setup

#### Install packages
```
sudo dnf update -y
sudo dnf install -y ansible-core
sudo dnf install -y wget git-core rsync vim nano git
sudo dnf install -y podman
sudo dnf install -y python3-pip
```

**Reboot** (recommended, not required):
```
sudo reboot
```

### Clone the repository

```bash
cd ~
git clone https://github.com/dynatrace-wwse/workshop-destination-automation.git
```

## AAP Provisioning

### Download AAP install tarball
Locate the `Ansible Automation Platform 2.6 Containerized Setup Bundle` for RHEL 9 at [https://access.redhat.com/downloads](https://access.redhat.com/downloads).
```
mkdir ~/redhat
cd ~/redhat
wget -O ansible-automation-platform-setup-bundle.tar.gz "<your-url-here>"
```

### Create ansible directory
```
export CURRENT_USER=$(whoami)
sudo mkdir /opt/ansible
sudo chown $CURRENT_USER:$CURRENT_USER /opt/ansible
```

### Prepare for AAP install
```
cd ~/workshop-destination-automation/ansible
export AAP_PUBLIC_HOSTNAME="<your-public-fqdn-here>"
export AAP_INSTALLER_LOCAL_PATH="$HOME/redhat/ansible-automation-platform-setup-bundle.tar.gz"
export AAP_ADMIN_PASSWORD="<your-strong-password-here>"
```

### Install ansible-galaxy collections
```
mkdir -p ~/.ansible/collections
ansible-galaxy collection install -r requirements.yml
```

### Run the installation playbook
```
ansible-playbook provision/playbooks/install_aap_containerized.yml
```

### Apply your subscription license via the AAP web interface

### Provision execution environments (build or import)

You can build images locally from source images with ansible-builder:
```
ansible-playbook provision/playbooks/build_custom_ee.yml
```

```
ansible-playbook provision/playbooks/build_custom_de.yml
```

You can import images created specifically for this workshop:
```
ansible-playbook provision/playbooks/import_custom_ee.yml
```
This will use: ghcr.io/dynatrace-wwse/destination-automation-oneagent-ee and ghcr.io/dynatrace-wwse/destination-automation-podman-ee

```
ansible-playbook provision/playbooks/import_custom_ee.yml
```
This will use: ghcr.io/dynatrace-wwse/destination-automation-dt-de

## AAP Deployment

### Create AAP Service Account user
Steps documented in deploy/docs/aap-service-account-setup.md

### Deploy AAP Config Objects
```
ansible-playbook deploy/playbooks/configure_aap.yml
```

### Deploy EDA Config Objects
```
ansible-playbook deploy/playbooks/configure_eda.yml
```

### Apply Credentials via UI
- [ ] aap-service-account
- [ ] destination-automation-dynatrace-monaco
- [ ] destination-automation-dynatrace-oauth
- [ ] destination-automation-dynatrace-oneagent
- [ ] destination-automation-dynatrace-otlp 

### Create Job Template Domains (optional, recommended)
AAP Web UI -> Automation Execution (Automation Controller) -> Templates: Configure Domains (wrench icon)

- Name: Dynatrace
  - Labels: dynatrace
- Name: App
  - Labels: app

## Dynatrace Deployment

### Deploy Dynatrace Apps
destination-automation-deploy-dynatrace-apps

### Deploy Dynatrace API Config
destination-automation-deploy-dynatrace-api-config

### Deploy EdgeConnect
Use when running AAP & EDA on the workshop host that isn't publicly accessible
destination-automation-deploy-dynatrace-edgeconnect

### Deploy Monaco Project
destination-automation-deploy-dynatrace-monaco

### Configure OpenPipeline Routing Tables (Manually)
OpenPipeline routing tables are comprehensive for the environment.  If existing rules exist in the routing table, applying a new configuration with Monaco will delete/overwrite the existing rules.  To avoid this behavior, for now, routing table rules need to be created manually.

Logs:
- Name: destination-automation-logs
- Matching Condition: matchesValue(dt.host_group.id,"destination-automation")
- Pipeline: destination-automation-logs

Spans:
- Name: destination-automation-spans
- Matching Condition: matchesValue(dt.host_group.id,"destination-automation")
- Pipeline: destination-automation-spans

BizEvents:
- Name: destination-automation-bizevents
- Matching Condition: matchesValue(dt.host_group.id,"destination-automation")
- Pipeline: destination-automation-bizevents

### Enable Workflows Authorization
Open the Workflows app.  In the top right corner, click on the gear icon, then Authorization Settings.  Enable all Primary Permissions.  Enable all Secondary Permissions.  Click Save.

### Deploy OneAgent
desetination-automation-deploy-dynatrace-oneagent

Optionally remove sudo permissions from aap-service-account after successful installation

## Deploy App (easyTravel AI Travel Advisor Stack)

### Build Images and Deploy App
destination-automation-workflow-deploy-app-clean

The application is now reachable at the public hostname on http port 81
