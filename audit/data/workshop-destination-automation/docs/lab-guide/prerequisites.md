# Prerequisites

This section is written for workshop instructors preparing the environment before participants start the lab.

## Access and Accounts

- Access to a RHEL 9 (x86 arch) instance with internet egress
- Access to Red Hat Customer Portal for AAP installer downloads
- Access to a Red Hat Ansible Automation Platform 2.6 Subscription (1+ Node)
    - Trial license/subscription is OK
- Access to a Dynatrace tenant with full admin permissions to account and tenant
- Access to the workshop repository (public, read-only)

## Host Requirements

- RHEL 9 with full sudo privileges
- 8 CPU and 32 GB Memory and 100+ GB Disk
    - 200+ GB Disk recommended to avoid disk space management
- DNS or public hostname assigned for workshop access
- Port 22 (SSH) access required for workshop instructor
- Ports 443 (HTTPS) and 81 (HTTP) access required for workshop instructor and participants

## Required Inputs You Should Prepare

- AAP public hostname (public FQDN of RHEL 9 instance)
- AAP administrator password
- Dynatrace environment URL and required tokens
- Workshop service account details used by Controller and EDA

### Dynatrace Tokens and Credentials

Using full admin permissions to the Dynatrace account and tenant, create the following tokens/credentials for this workshop.  Store them in a secure location, as you will need to input them into the AAP web interface later.

!!! tip "Time Management Opportunity"
    Provisioning Red Hat Ansible Automation Platform will take some time (next step).  It is recommended to create these tokens while AAP is installing.

**Dynatrace Platform Token (Monaco)**

Use this token for Monaco configuration-as-code deployment.  This token is also used by Ansible roles that create Dynatrace configuration objects not handled by Monaco.

Reference: [Dynatrace Docs](https://docs.dynatrace.com/docs/deliver/configuration-as-code/monaco/guides/create-platform-token#create-a-platform-token)

Required scopes:

| Purpose | Scopes |
|---|---|
| Access platform metadata like Dynatrace classic URLs and version information | `app-engine:apps:run`, `app-engine:apps:install` |
| Manage Settings 2.0 objects and all-users permission | `settings:objects:read`, `settings:objects:write` |
| View Settings 2.0 schemas | `settings:schemas:read` |
| Manage automation workflows | `automation:workflows:read`, `automation:workflows:write`, `automation:calendars:read`, `automation:calendars:write`, `automation:rules:read`, `automation:rules:write` |
| Access all automation workflows | `automation:workflows:admin` |
| Manage documents | `document:documents:read`, `document:documents:write`, `document:documents:delete`, `document:trash.documents:read`, `document:trash.documents:delete` |

**Dynatrace OTLP API token**

Used by the OpenTelemetry Collector export in workshop jobs.

Reference: [Dynatrace Docs](https://docs.dynatrace.com/docs/shortlink/otel-getstarted-otlpexport#authentication-export-to-activegate)

| Signal | Scope required |
|---|---|
| Traces | `openTelemetryTrace.ingest` |
| Metrics | `metrics.ingest` |
| Logs | `logs.ingest` |

Provide in AAP credential fields:
- `DT_OTLP_ENDPOINT`
- `DT_OTLP_TOKEN`

Minimum token scopes must match the signals you export (metrics, traces, logs) in your Dynatrace tenant.

**Dynatrace OAuth client credentials (EdgeConnect)**

Primarily used to deploy the EdgeConnect container on the AAP host.  This credential needs permissions to read, write, and manage EdgeConnects and their OAuth clients.

Required scopes:

| Purpose | Scopes |
|---|---|
| Manage EdgeConnect configurations | `app-engine:edge-connects:read`, `app-engine:edge-connects:write`, `app-engine:edge-connects:delete` |
| Manage EdgeConnect OAuth | `oauth2:clients:manage` |

Provide in AAP credential fields:
- `DT_OAUTH_CLIENT_ID`
- `DT_OAUTH_CLIENT_SECRET`
- `DT_OAUTH_ACCOUNT_URN`

**Dynatrace OneAgent PaaS Token**

Used by OneAgent deployment automation.

Required scopes:

| Purpose | Scopes |
|---|---|
| PaaS integration | `InstallerDownload`, `SupportAlert` |

Provide in AAP credential fields:
- `oneagent_environment_url`
- `oneagent_paas_token`

## Instructor Readiness Checklist

- [ ] Verify connectivity to required registries and APIs
- [ ] Confirm workshop host has available CPU, memory, and disk for containers
- [ ] Validate that port access policies allow workshop traffic
- [ ] Confirm you are able to create all tokens/credentials

Continue to [Provision](provision.md).
