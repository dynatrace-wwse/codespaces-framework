# IAM role: `OrbitalFleetAutoscaler`

**Created** 2026-08-13 00:39:04 UTC · **Account** 112258687663 (`dynatrace-emea`) ·
**Region in use** eu-west-2 · **Requested by** Sergio Hinojosa (hj.sergio / sergio.hinojosa@dynatrace.com)

This document exists so Dynatrace IT (EDE) can review exactly what was created, why, and what
its blast radius is. Everything below is reproducible from the account.

---

## 1. Why this role exists

The Orbital enablement platform runs student training environments on a small EC2 worker fleet.
Workers must be added before a bootcamp and removed after it, otherwise we either run out of
capacity mid-session or pay for idle machines.

Before this role, the only credential available to the ops server was a **federated STS session
token** pasted in by hand (`arn:aws:sts::112258687663:assumed-role/dtRoleRegionsAdvancedUser/…`).
That approach has three problems:

1. **It expires roughly hourly.** Any unattended scale-up or scale-down fails as soon as the token
   dies — including a scale-*down*, so the failure mode costs money.
2. **It carries a human's full entitlements.** `dtRoleRegionsAdvancedUser` can act on *every*
   instance in the account. A bug in the autoscaler could have terminated unrelated production
   instances.
3. **It requires a human to be awake.** No scheduled or automatic capacity management is possible.

The role replaces that with a scoped, non-expiring machine identity.

## 2. What was created

| Object | Name / ARN |
|---|---|
| IAM role | `arn:aws:iam::112258687663:role/OrbitalFleetAutoscaler` |
| Inline policy | `OrbitalFleetAutoscalerPolicy` (inline — no managed policies attached) |
| Instance profile | `arn:aws:iam::112258687663:instance-profile/OrbitalFleetAutoscaler` |
| Attached to | `i-002cd25131b403065` — "autonomous-enablements" (Orbital master, c7g.4xlarge, eu-west-2c) |
| Association id | `iip-assoc-03fbe01b4da5f73c2` |

Role tags: `Owner=sergio.hinojosa@dynatrace.com`, `Purpose=orbital-worker-autoscaling`,
`ManagedBy=enablement-framework`, `CostCenter=WWSE-enablement`.

**Trust policy** — only the EC2 service may assume it. No human, no other account, no other
service can:

```json
{"Version":"2012-10-17","Statement":[{"Sid":"AllowEC2InstancesToAssume","Effect":"Allow",
 "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
```

In practice this means: credentials are obtainable **only** from the instance metadata service of
an EC2 instance the profile is attached to — today exactly one instance, the Orbital master.

## 3. The permission boundary

The policy has six statements. The security property is that **every mutating action is
conditioned on a tag**, so the role can only ever act on instances the autoscaler itself created.

| Sid | Effect | Actions | Scope |
|---|---|---|---|
| `ReadOnlyDiscovery` | Allow | `ec2:Describe*` (instances, types, images, subnets, SGs, volumes, AZs, tags, key-pairs, spot prices, regions), `servicequotas:GetServiceQuota`, `servicequotas:ListServiceQuotas` | `*` — read-only |
| `LaunchOnlyTaggedFleetInstances` | Allow | `ec2:RunInstances` | `instance/*` **only if** `aws:RequestTag/ManagedBy = orbital-autoscaler` |
| `LaunchSupportingResources` | Allow | `ec2:RunInstances` | image / snapshot / subnet / security-group / volume / ENI / key-pair / placement-group |
| `TagOnlyAtLaunch` | Allow | `ec2:CreateTags` | **only if** `ec2:CreateAction = RunInstances` — cannot retag anything after the fact |
| `LifecycleOnlyOnFleetTaggedInstances` | Allow | `ec2:TerminateInstances`, `StopInstances`, `StartInstances`, `ModifyInstanceAttribute` | **only if** `ec2:ResourceTag/ManagedBy = orbital-autoscaler` |
| `HardDenyTouchingTheMasterItself` | **Deny** | `ec2:TerminateInstances`, `StopInstances`, `ModifyInstanceAttribute` | `instance/i-002cd25131b403065` |

Three deliberate design choices worth noting in review:

- **`TagOnlyAtLaunch` is what makes the tag scoping meaningful.** If the role could call
  `CreateTags` freely, it could tag *any* instance in the account with `ManagedBy=orbital-autoscaler`
  and then terminate it. Restricting `CreateTags` to `ec2:CreateAction=RunInstances` closes that
  privilege-escalation path.
- **The explicit `Deny` on the master is redundant but intentional.** The master is not tagged, so
  the tag condition already excludes it. The `Deny` makes the guarantee independent of tag hygiene —
  an explicit Deny cannot be overridden by any future Allow.
- **No `iam:PassRole`.** The role cannot attach instance profiles to the instances it launches, so
  it cannot mint a more privileged machine identity than itself.

## 4. Verification evidence

Run 2026-08-13 00:41 UTC from the master, authenticating **as the instance role**
(`arn:aws:sts::112258687663:assumed-role/OrbitalFleetAutoscaler/i-002cd25131b403065`).
All checks used `--dry-run`, which performs full IAM evaluation without changing state.

```
=== POSITIVE: fleet-tagged workers (expect ALLOWED) ===
stop tagged worker amd001                                  ALLOWED
start tagged worker amd002                                 ALLOWED

=== NEGATIVE: expect DENIED ===
terminate MASTER (explicit Deny)                           DENIED
stop MASTER (explicit Deny)                                DENIED
terminate 'Production Dynatrace Success HUB'               DENIED
terminate 'EMEA Managed Node1' (untagged)                  DENIED
stop 'agentic-development' (untagged)                      DENIED

RunInstances WITHOUT ManagedBy tag                         UnauthorizedOperation
RunInstances WITH    ManagedBy tag                         DryRunOperation (allowed)
```

Reproduce any row with:

```bash
sudo -u ops /usr/local/bin/aws ec2 terminate-instances \
  --instance-ids <id> --region eu-west-2 --dry-run
# DryRunOperation = would be permitted · UnauthorizedOperation = denied
```

## 5. What EDE may want to know

- **Instances this role creates are tagged and attributable.** Every launch carries
  `ManagedBy=orbital-autoscaler`, `Owner=sergio.hinojosa@dynatrace.com`,
  `project=autonomous-enablements`, `orbital-role=worker`. They are visible in Config, Cost
  Explorer and any tag-based inventory.
- **The account's on-demand standard vCPU quota is 3264** (`L-1216C47A`, read 2026-08-13). The
  bootcamp needs ~48. The autoscaler pre-flights against this quota and refuses to exceed it.
- **The federated user's own role could already do all of this and more.** This role is strictly
  *narrower* than the human credential it replaces; it does not grant the platform any capability
  the operator did not already have.
- **Cost controls in the platform, independent of IAM:** a fleet-wide freeze switch, a per-call
  launch cap, a maximum fleet size, an in-flight launch registry that prevents runaway loops, and
  an optional self-terminating instance lifetime (`shutdown -h +N` armed in user-data plus
  `instance-initiated-shutdown-behavior=terminate`) so a worker disappears on schedule even if the
  control plane is entirely dead.
- **Two things this role cannot do that a reviewer might assume it can:** it cannot create or
  modify IAM objects, and it cannot touch any instance it did not itself launch.

## 6. Revocation

To disable the capability instantly, without touching the Orbital service:

```bash
# 1. Detach the profile from the master (autoscaling stops immediately)
aws ec2 disassociate-iam-instance-profile --association-id iip-assoc-03fbe01b4da5f73c2 --region eu-west-2

# 2. Or delete the permissions but keep the audit trail
aws iam delete-role-policy --role-name OrbitalFleetAutoscaler --policy-name OrbitalFleetAutoscalerPolicy

# 3. Full removal
aws iam remove-role-from-instance-profile --instance-profile-name OrbitalFleetAutoscaler --role-name OrbitalFleetAutoscaler
aws iam delete-instance-profile --instance-profile-name OrbitalFleetAutoscaler
aws iam delete-role --role-name OrbitalFleetAutoscaler
```

Orbital degrades cleanly: `check_credentials()` reports "no credentials" in the UI and every
scale action refuses. Running training sessions are unaffected — they do not use AWS credentials.

## 7. Open item to raise with EDE

The federated role `dtRoleRegionsAdvancedUser` **can create IAM roles and instance profiles**
(verified — this role was created with it). If that is not the intended entitlement for a
regions-advanced-user, EDE should know; it was not obvious from the role name, and it is the
capability that made this change possible without a ticket.

Source of truth for the policy JSON: `ops-server/dashboard/fleet.py` (`FLEET_TAG_KEY` /
`FLEET_TAG_VALUE` must match the policy conditions) and this document.
