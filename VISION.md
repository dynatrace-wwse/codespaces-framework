# Enablement Framework: Mastering Complexity Through Open Knowledge

## The Problem

Observability platforms are powerful but complex. Every new capability adds cognitive load. Sales engineers, partners, and customers need hands-on experience to master these tools — but building and maintaining training at scale is its own engineering problem. Outdated labs, broken dependencies, inconsistent environments, and manual setup friction erode trust: if the training doesn't work, the platform looks unreliable.

## The Vision

An open-source framework that turns training repositories into a managed fleet of self-service, self-healing environments. Every lab runs identically as a GitHub Codespace, a Dev Container, or a local Docker setup. One click, zero friction.

Three principles: **separation of concerns** — core functions maintained centrally, repos contain only custom content. **Version-pinned reproducibility** — every repo locks its framework version, updates flow through automated sync. **Observable by default** — every instantiation, every page view, every error is tracked.

## How It Works

A universal container image provides the runtime. A sync tool manages the fleet — one command bumps the version, migrates all repos, runs tests, and merges. Templates let anyone create a new enablement in hours: fork, write content, deploy. The framework handles platform integration, app exposure, monitoring, documentation, and CI/CD.

Every deployed app gets automatic Real User Monitoring through shared ingress. One architectural decision benefits every repo instantly.

## Closed-Loop Monitoring

Every environment creation is geo-enriched and tracked. A live dashboard shows global adoption by country and city, error rates per repo, CI health across the fleet, and documentation engagement. When something breaks after an update, the team knows in minutes — not weeks.

Nightly integration tests validate the full stack. Failures auto-create GitHub issues with context and suggested fixes. Straightforward regressions generate automated pull requests.

## Customer Value

**Sales engineers** create enablements in hours, not weeks. **Partners and customers** get consistent, working labs — no local tooling, no "works on my machine." **The organization** maintains a growing fleet with a small team, where new platform features reach all training through a single release.

## Open Knowledge

The framework, the tooling, the monitoring — all open-source. Complexity isn't mastered through documentation. It's mastered through working systems people can inspect, run, and improve. The framework doesn't just teach a platform — it demonstrates how to build observable, self-healing infrastructure.

Open knowledge scales. Closed knowledge doesn't.

####
Dynatrace Enablement Framework in a Nutshell
The Dynatrace Enablement Framework simplifies the delivery of demos and hands-on trainings for the Dynatrace Platform. It provides a unified set of tools, templates, and best practices to ensure enablements are easy to create, run anywhere, and maintain over time.

✅ Key Features#
GitHub-Hosted & Versioned
All trainings are managed in GitHub repositories, ensuring traceability and collaboration.

Self-Service Documentation
Each repo includes its own MkDocs-powered documentation, published via GitHub Pages.

Universal Base Image
A Docker image supports AMD/ARM architectures, GitHub Codespaces, VS Code Dev Containers, and containerized execution in any Ubuntu OS.

Separation of Concerns
Modular design allows repo-specific logic without impacting the core framework.

Automated Testing
GitHub Actions enable end-to-end integration tests for all trainings.

Monitoring & Analytics
Usage and adoption are tracked with Dynatrace for continuous improvement.

Rapid Training Creation
Templates and automation help trainers launch new enablement content quickly.

Centralized Maintenance
The Codespaces Synchronizer tool keeps all repositories up to date with the latest framework changes.

🤲 Benefits#
Reduces complexity and friction for trainers and learners
Increases adoption and consistency
Scales across internal, partner, and customer enablement a Kubernetes cluster.


-----
Where are we and what we want to build next.

 I want to build the cicd server for the enablement framework. The codespaces-framework is the root repo for synchronizing and maintaining a fleet of workshops and hands-on trainings.
  TO understand more about it read VISION.md.  The framework uses cache and is tagged/versioned. We just build a CICD server, for using the synchronizer (in its directory) and also
  building and testing the repos. The CICD server is now live at https://autonomous-enablements.whydevslovedynatrace.com (which is this server, this is master and has a worker) more
  inthe directory ops-server. I want you to help me create a great UI for tracking the builds that are in progress. The framework also publsihes the repos in a yaml file that are manged by it and its
  published in here: https://dynatrace-wwse.github.io. I want to use that slick design also for the ops server. We are at the verge of testing and merge the last changes of the codespaces-framework (dt-operator and dynakube refactoring and k3d implementation) to all managed repos. I want you to help me test that. 

  We have done great progress on the ops-server, it can run multiple repos simultaneously using a docker-in-docker approach isolating each environment and utilizing the ressources of the server very effectively. In PROJECT-STATUS.md you can find what we wanted to do and we improved it by adding the isolation using docker-in-docker. we want to improve the UI of the ops-server and optimize it, adding more functionality like listing all past builds, which repo, architecture, time it took to run/compile and if it passed or failed. a test for a repo with the same architecture and branch should NOT run simmultaneously. There is a bug displaying the logs of a repo (I believe is when mutliple integration test ran summultaneously for the same arch and branch). In the nightly builds it still shows no colours, it would be great to have it consistently showing the logs as in the fleet in a window. We should add in that window the possibility then to open the log in a separate window. The branch on the fleet should show as a dropdown where we dinamycally show the branches. we should add a link to the repo, the release its at. We should move the audit to the ops-server. The audit is generated by teh synchronizer and published here https://dynatrace-wwse.github.io/audit.html but the better place is in the ops-server. The idea to this server is to validate nighltly the repos, verify that they work and also to test changes easily across all repos so we can refactor and ship fast. 