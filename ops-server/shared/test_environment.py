"""Properties of the environment boundary (shared/environment.py).

These are not tests of convenience helpers. Staging and production share one
AWS account, and the predicate these pin is what decides whether one
environment's control loop may terminate the other's machines.

Runnable two ways:
  - pytest:     python3 -m pytest shared/test_environment.py
  - standalone: /home/ops/ops-venv/bin/python -m shared.test_environment
"""

import os
from contextlib import contextmanager

from shared import environment as env


@contextmanager
def running_as(name: str | None):
    """Run the block as if ORBITAL_ENV were ``name`` (None = unset)."""
    prev = os.environ.get("ORBITAL_ENV")
    if name is None:
        os.environ.pop("ORBITAL_ENV", None)
    else:
        os.environ["ORBITAL_ENV"] = name
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("ORBITAL_ENV", None)
        else:
            os.environ["ORBITAL_ENV"] = prev


# ── Selection ────────────────────────────────────────────────────────────────

def test_unset_orbital_env_is_production():
    # Every existing deployment has no ORBITAL_ENV. Defaulting anywhere but
    # prod would silently change what the running fleet believes it owns.
    with running_as(None):
        assert env.current().name == env.PROD


def test_explicit_selection():
    with running_as("staging"):
        assert env.current().name == env.STAGING
    with running_as("PROD"):          # case/whitespace tolerant
        assert env.current().name == env.PROD
    with running_as("  staging  "):
        assert env.current().name == env.STAGING


def test_unknown_environment_is_refused_loudly():
    # Silently falling back to prod on a typo would give a staging host
    # production's identity — the exact failure this module exists to prevent.
    with running_as("stagng"):
        try:
            env.current()
        except ValueError as e:
            assert "stagng" in str(e)
        else:
            raise AssertionError("a typo'd ORBITAL_ENV must not resolve")


# ── Ownership ────────────────────────────────────────────────────────────────

def test_untagged_instance_belongs_to_production():
    # The four long-lived machines carry no env tag and cannot be tagged by the
    # autoscaler (its CreateTags grant is conditioned on RunInstances). If
    # untagged read as anything else, prod would go blind the moment this
    # shipped and staging could claim the master.
    assert env.instance_env({}) == env.PROD
    assert env.instance_env({"Name": "autonomous-enablements-worker"}) == env.PROD
    assert env.instance_env({"env": ""}) == env.PROD
    assert env.instance_env(None) == env.PROD


def test_ownership_is_a_partition():
    # An instance belongs to exactly one environment. If this ever holds true
    # for both, every isolation guarantee below is void.
    for tags in ({}, {"env": "prod"}, {"env": "staging"}, {"env": ""}):
        owners = [e for e in env.KNOWN_ENVS if env.owns(tags, e)]
        assert len(owners) == 1, f"{tags} claimed by {owners}"


def test_staging_never_owns_a_legacy_or_prod_machine():
    for tags in ({}, {"env": "prod"}, {"Name": "autonomous-enablements-worker"}):
        assert not env.owns(tags, env.STAGING)


def test_prod_never_owns_a_staging_machine():
    assert not env.owns({"env": "staging"}, env.PROD)


# ── Environment definitions ──────────────────────────────────────────────────

def test_environments_are_distinct_where_it_matters():
    prod, staging = env.get("prod"), env.get("staging")
    # Sharing any of these between environments recreates the coupling the
    # whole split exists to remove.
    for field in ("ec2_tag", "public_url", "code_branch", "app_tenant", "iam_role"):
        assert getattr(prod, field) != getattr(staging, field), \
            f"prod and staging share {field}"


def test_staging_has_no_production_template_instance():
    # Inheriting prod worker-1's subnet/security-groups by default would put
    # staging workers inside production's networking on their first launch.
    assert env.get("staging").template_instance_id != env.get("prod").template_instance_id
    assert env.get("staging").template_instance_id == ""


def test_prod_tracks_the_promotion_branch_and_staging_tracks_main():
    assert env.get("prod").code_branch == "production"
    assert env.get("staging").code_branch == "main"


def test_environment_is_immutable():
    # Anything holding a reference must not be able to repoint another
    # component's environment underneath it.
    try:
        env.get("prod").name = "staging"          # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Environment must be frozen")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as exc:                       # noqa: BLE001
                failures += 1
                print(f"  FAIL {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'PASS'} — {failures} failure(s)")
    raise SystemExit(1 if failures else 0)
