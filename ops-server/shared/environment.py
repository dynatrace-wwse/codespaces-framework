"""The environment an Orbital process belongs to, as one typed object.

Orbital has run as a single environment: one host that is simultaneously the
development machine, the staging machine and production. Standing up staging in
the same AWS account makes one question load-bearing that has never had to be
asked before — *does this machine belong to me?* — because
``dashboard/fleet.py`` discovers instances by ``tag:project`` and
``tag:orbital-pool``, neither of which distinguishes prod from staging. A
staging control loop pointed at that account would list, and reap, production
workers.

Everything environment-specific derives from one frozen object selected once
from ``ORBITAL_ENV``, so adding a third environment later is a config entry
rather than a migration.

Two rules govern ownership, and both exist to fail in the safe direction:

**An untagged instance belongs to production.** The four long-lived machines
predate the tag and cannot be tagged by the autoscaler itself — its
``ec2:CreateTags`` grant is conditioned on ``ec2:CreateAction=RunInstances``,
so it may only tag what it launches. Defaulting untagged to prod means staging
can never claim one of them, and prod does not go blind the moment this code
ships but before the backfill runs. The reverse default would do both of the
things this module exists to prevent.

**Ownership is checked in code as well as in IAM.** The IAM role's tag
condition is the real boundary — a cross-environment terminate returns
``UnauthorizedOperation`` regardless of what the code believes. The checks here
are defence in depth for the case where a discovery filter is added without its
environment scope, which is exactly the mistake this design is most exposed to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The tag key carrying the environment on every instance the autoscaler launches.
ENV_TAG_KEY = "env"

# What an instance with no `env` tag is taken to be. See the module docstring:
# this default is what keeps production working before the backfill and keeps
# staging away from the machines it must never touch.
LEGACY_ENV = "prod"

PROD = "prod"
STAGING = "staging"
KNOWN_ENVS = (PROD, STAGING)


@dataclass(frozen=True)
class Environment:
    """Everything that differs between prod and staging, in one place."""

    name: str
    # Value of the `env` tag on instances this environment owns.
    ec2_tag: str
    # Redis is a SEPARATE INSTANCE per environment, not a key prefix. A prefix
    # would share a memory limit, one persistence file, and the blast radius of
    # a single FLUSHDB.
    redis_url: str
    public_url: str
    # Which branch launched workers reset --hard to at boot (fleet.py builds
    # this into user-data). prod tracks the promotion branch; staging tracks main.
    code_branch: str
    # Tenant this environment deploys the Enablement App to.
    app_tenant: str
    # Instance profile / role whose tag condition enforces the boundary.
    iam_role: str
    # Instance whose subnet / security groups / key-name new workers inherit.
    template_instance_id: str

    @property
    def is_prod(self) -> bool:
        return self.name == PROD


_ENVIRONMENTS = {
    PROD: Environment(
        name=PROD,
        ec2_tag=PROD,
        redis_url="redis://localhost:6379/0",
        public_url="https://autonomous-enablements.whydevslovedynatrace.com",
        code_branch="production",
        app_tenant="https://geu80787.apps.dynatrace.com",
        iam_role="OrbitalFleetAutoscaler",
        template_instance_id="i-02b773319c758fe40",
    ),
    STAGING: Environment(
        name=STAGING,
        ec2_tag=STAGING,
        redis_url="redis://localhost:6379/0",
        public_url="https://staging.autonomous-enablements.whydevslovedynatrace.com",
        code_branch="main",
        app_tenant="https://ydi9582h.sprint.apps.dynatracelabs.com",
        iam_role="OrbitalFleetAutoscalerStaging",
        # Set once the staging worker exists; until then staging must not be
        # able to inherit production worker-1's networking by accident.
        template_instance_id="",
    ),
}


def current() -> Environment:
    """The environment this process is running as.

    Defaults to production. An unset ``ORBITAL_ENV`` is the production host —
    every existing deployment — so defaulting anywhere else would silently
    change what the running fleet believes it owns.
    """
    return get(os.environ.get("ORBITAL_ENV", PROD))


def get(name: str) -> Environment:
    key = (name or "").strip().lower()
    if key not in _ENVIRONMENTS:
        raise ValueError(
            f"unknown ORBITAL_ENV {name!r} — expected one of {', '.join(KNOWN_ENVS)}"
        )
    return _ENVIRONMENTS[key]


def instance_env(tags: dict) -> str:
    """The environment an instance belongs to, from its flattened tags.

    An absent or empty tag reads as :data:`LEGACY_ENV`.
    """
    return (tags or {}).get(ENV_TAG_KEY) or LEGACY_ENV


def owns(tags: dict, env_name: str) -> bool:
    """True when the instance described by ``tags`` belongs to ``env_name``.

    This is the single predicate every discovery path and every mutating guard
    must agree on. Two properties it is required to have, both pinned by tests:

    - it is a partition — an instance belongs to exactly one environment, so
      ``owns(t, "prod")`` and ``owns(t, "staging")`` are never both true;
    - untagged is prod, so staging cannot claim a legacy machine.
    """
    return instance_env(tags) == (env_name or "").strip().lower()
