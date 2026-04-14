# Clean

Use this section to return the workshop environment to a known baseline after completion.

## Objectives

- Stop and remove workshop application resources.
- Remove temporary automation artifacts where applicable.
- Confirm no unexpected services or credentials remain active.

## Cleanup Flow

## 1. Run Workshop Cleanup Automation

- Use the provided clean playbooks or AAP job templates in the clean function.
- Remove deployed application containers, networks, and temporary files.

## 2. Optional Dynatrace Cleanup

- Remove workshop-only monitoring objects, app installs, and test entities.
- Retain shared tenant configuration only if needed by your organization.

## 3. Optional AAP Cleanup

- Disable or remove temporary credentials and service accounts used for the workshop.
- Archive or delete workshop-specific projects and job templates if this was a disposable environment.

## 4. Host Validation

- Verify expected ports are closed.
- Confirm no workshop containers are still running.
- Confirm disk and memory utilization has returned to expected idle levels.

## Post-Lab Checklist

- [ ] Workshop application stack removed.
- [ ] No dangling Podman resources.
- [ ] Temporary secrets reviewed and rotated as needed.
- [ ] Environment ready for next workshop or decommission.

Return to [Overview](overview.md).
