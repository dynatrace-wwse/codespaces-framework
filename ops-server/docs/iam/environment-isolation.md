# IAM: the environment boundary

> **STATUS: APPLIED 2026-08-26.** Tags backfilled onto all four long-lived
> instances, `env` conditions live on `OrbitalFleetAutoscalerPolicy`, and
> `OrbitalFleetAutoscalerStaging` created with its own instance profile.
> Verification results are at the bottom of this file.

Staging and production share AWS account `112258687663`. The tag filters in
`dashboard/fleet.py` and `dashboard/workshop_fleet.py` are defence in depth —
**IAM is the actual boundary.** A cross-environment terminate must come back
`UnauthorizedOperation` regardless of what the calling code believes it is
doing, because the failure mode being defended against is precisely a code
path that believes wrongly.

## Ordering — this bites if you get it wrong

1. **Deploy the code.** Safe at any time: `shared/environment.py` reads an
   untagged instance as production, so nothing changes for the running fleet.
2. **Backfill the tags** — `tools/fleet/backfill-env-tags.sh --apply`. Needs a
   human credential: the autoscaler's `ec2:CreateTags` is conditioned on
   `ec2:CreateAction=RunInstances`, so it can only tag what it launches.
3. **Verify every fleet instance reports `env`.**
4. **Only then** add the `env` condition to the production role.

Doing 4 before 2 leaves production able to *see* its four long-lived machines
but unable to stop or terminate them — the autoscaler would refuse every
scale-down with `UnauthorizedOperation` and the fleet would only ever grow.

## Production — add to `OrbitalFleetAutoscalerPolicy`

The existing policy already scopes `RunInstances` to
`aws:RequestTag/ManagedBy = orbital-autoscaler`. Add the environment as a
second required tag on launch, and as a required resource tag on every
mutating action.

```json
{
  "Sid": "LaunchOnlyTaggedFleetInstances",
  "Effect": "Allow",
  "Action": "ec2:RunInstances",
  "Resource": "arn:aws:ec2:*:112258687663:instance/*",
  "Condition": {
    "StringEquals": {
      "aws:RequestTag/ManagedBy": "orbital-autoscaler",
      "aws:RequestTag/env": "prod"
    }
  }
},
{
  "Sid": "MutateOnlyOwnEnvironment",
  "Effect": "Allow",
  "Action": [
    "ec2:TerminateInstances",
    "ec2:StopInstances",
    "ec2:StartInstances"
  ],
  "Resource": "arn:aws:ec2:*:112258687663:instance/*",
  "Condition": {
    "StringEquals": {
      "ec2:ResourceTag/ManagedBy": "orbital-autoscaler",
      "ec2:ResourceTag/env": "prod"
    }
  }
}
```

> `aws:RequestTag` constrains what a launch may *ask for*; `ec2:ResourceTag`
> constrains what an action may be *applied to*. Both are needed: the first
> stops an untagged (and therefore unreapable) launch, the second stops a
> cross-environment terminate.

## Staging — new role `OrbitalFleetAutoscalerStaging`

Identical, with `staging` substituted throughout, attached to the staging
host's instance profile. `ReadOnlyDiscovery` (all the `ec2:Describe*` calls)
stays unconditioned in both roles: describes are account-wide by design and
cannot be tag-scoped, which is exactly why the code filters client-side.

## The property to verify after each change

Not "does my own environment still work" — that regresses visibly and someone
notices. The one that fails silently is the other direction:

```bash
# From the STAGING host, against a PRODUCTION instance id.
# Required outcome: UnauthorizedOperation.
aws ec2 stop-instances --region eu-west-2 --instance-ids <a-prod-instance>
```

A success here means the boundary does not exist, whatever the tests say.

## Why describes stay open

`ec2:DescribeInstances` takes no resource ARN and cannot be conditioned on
tags. Every environment can therefore *see* every instance in the account.
That is unavoidable, and it is the reason `list_fleet` and `_instances_tagged`
filter in Python rather than relying on the API to hand back only what they
own — see the module docstring in `shared/environment.py`.


---

## Applied state (2026-08-26)

**Roles**

| Role | Instance profile | Scope |
|---|---|---|
| `OrbitalFleetAutoscaler` | (existing, on the prod master) | `env=prod` |
| `OrbitalFleetAutoscalerStaging` | `OrbitalFleetAutoscalerStaging` | `env=staging` |

Both carry the same six statements. The staging copy renames the master deny to
`HardDenyTouchingTheProductionMaster` and keeps it: staging must not be able to
stop the production master even if the environment condition were ever relaxed.

**Backfill.** All four long-lived instances now carry `env=prod`
(master, worker-1, worker-2, and the stopped worker-3). Note that only worker-1
and worker-2 carry `ManagedBy=orbital-autoscaler`; the master and worker-3 do
not, so the lifecycle statement never granted on them in the first place — the
master additionally has an explicit `Deny`.

**Verified with `aws iam simulate-principal-policy`,** which evaluates the real
policy without assuming the role or touching an instance:

| Principal | Action | Target | Result |
|---|---|---|---|
| prod | Terminate | prod worker | `allowed` |
| **staging** | **Terminate** | **prod worker** | **`implicitDeny`** |
| staging | Terminate | staging worker | `allowed` |
| **prod** | **Terminate** | **staging worker** | **`implicitDeny`** |
| **staging** | **Stop** | **prod worker** | **`implicitDeny`** |
| prod | Stop | prod worker | `allowed` |
| prod | RunInstances | `env=prod` tag | `allowed` |
| **prod** | **RunInstances** | **no env tag** | **`implicitDeny`** |
| **prod** | **RunInstances** | **`env=staging` tag** | **`implicitDeny`** |
| staging | RunInstances | `env=staging` tag | `allowed` |
| **staging** | **RunInstances** | **`env=prod` tag** | **`implicitDeny`** |

The untagged-launch denial matters as much as the cross-environment ones: a
machine launched without an `env` tag is one this role could create but never
afterwards terminate, so it would run to its in-instance `shutdown -h +N`
backstop with nothing able to reap it.

**Production after the change:** all 4 instances still visible, control loop
reporting zero `UnauthorizedOperation`, API 200.

**Rollback.** The pre-change policy document was kept at
`/tmp/fleet-policy-ROLLBACK.json` during the session; to reconstruct it, remove
the two `env` keys from `LaunchOnlyTaggedFleetInstances` and
`LifecycleOnlyOnFleetTaggedInstances`. Nothing else was modified.
