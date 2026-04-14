# Remediate

This phase demonstrates autonomous incident response: a model-related change introduces drift, Dynatrace detects impact, and Event-Driven Ansible restores a healthy state without manual intervention.

## Objectives

- Enable a feature flag that changes the AI embedding model.
- Observe impact without taking down the application.
- Validate Dynatrace detection of model drift and resulting problem state.
- Trigger EDA-driven remediation that restores known-good behavior.

## Step 1: Introduce Controlled Change

- Enable the embedding model feature flag.
- Confirm the application remains available.
- Continue sending prompts to generate post-change telemetry.

## Step 2: Observe Detection and Eventing

- Verify Dynatrace detects drift or quality degradation.
- Confirm the problem event is emitted to the configured webhook path.
- Validate EDA receives and matches the event.

## Step 3: Automated Recovery

- EDA triggers the remediation automation.
- Automation rolls configuration back to a healthy model path.
- Post-remediation checks verify application behavior and health.

## Validation

- [ ] No outage occurred during the model change.
- [ ] Drift/problem was detected in Dynatrace.
- [ ] EDA automation executed successfully from event trigger.
- [ ] Service health and response quality returned to expected baseline.

Continue to [Summarize](summarize.md).
