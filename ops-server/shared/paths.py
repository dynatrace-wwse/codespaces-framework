"""Where Orbital's own code and the framework it reads live, as one answer.

Orbital is currently a subdirectory of the public ``codespaces-framework``
checkout, and eight modules encode that fact as a literal path. Splitting
Orbital into its own private repository moves it to ``/home/ops/orbital`` while
the framework stays where it is — so every one of those literals becomes wrong
on the same day, and the ones that fail quietly are worse than the ones that
crash.

Two different questions are being asked, and conflating them is the trap this
module exists to prevent:

**"Where is Orbital's code?"** — :func:`fleet_host_orbital_checkout`. Answered
as an ABSOLUTE path because its only caller bakes it into a cloud-init script
that runs *as root on a different machine*. Rooting it at ``Path.home()`` would
resolve against whoever generated the script — the dashboard runs as ``ops``,
but a developer running the same function gets ``/home/ubuntu`` silently
written into a worker's boot script.

**"Where is the framework this process should read?"** — :func:`framework_dir`.
Rooted at ``Path.home()``, because it describes the calling process's own view
and every existing caller already resolves it that way.

Defaults are today's layout, so importing this changes nothing. The split is
then a matter of setting two environment variables in ``/home/ops/.env`` and the
systemd units, which is a config change that can be staged host by host and
reverted without a deploy.
"""

import os
from pathlib import Path

# Today's layout: Orbital lives inside the framework checkout, and every fleet
# host uses the identical path (see the deployment section of ops-server's
# CLAUDE.md — master and both workers are deliberately the same).
DEFAULT_FLEET_HOST_CHECKOUT = "/home/ops/enablement-framework/codespaces-framework"
# Relative to the calling process's home, which is how webhook/config.py,
# workers/manager.py and tools/agentic_validator.py have always resolved it.
DEFAULT_FRAMEWORK_SUBPATH = ("enablement-framework", "codespaces-framework")

ORBITAL_CHECKOUT_ENV = "ORBITAL_CHECKOUT"
FRAMEWORK_DIR_ENV = "FRAMEWORK_DIR"
APP_REPO_DIR_ENV = "APP_REPO_DIR"


def fleet_host_orbital_checkout() -> str:
    """Absolute path to Orbital's code on a fleet host, as a string.

    A string rather than a Path because it is interpolated into a shell script.
    Read at call time, not at import, so a test can set the variable without
    reloading the module.
    """
    return os.environ.get(ORBITAL_CHECKOUT_ENV) or DEFAULT_FLEET_HOST_CHECKOUT


def framework_dir() -> Path:
    """Path to the ``codespaces-framework`` clone this process should read.

    After the split the framework remains a separate sibling clone that Orbital
    consumes read-only — it is where ``repos.yaml``, ``sync.cli`` and
    ``.env-qa`` live, none of which move.
    """
    override = os.environ.get(FRAMEWORK_DIR_ENV)
    if override:
        return Path(override)
    return Path.home().joinpath(*DEFAULT_FRAMEWORK_SUBPATH)


def app_repo_dir() -> Path | None:
    """Path to the ``dynatrace-app-enablements`` clone, or None if absent.

    Returns None only when the repo genuinely is not available — a checkout that
    does not have it (CI, a fresh clone) is a legitimate state. But an
    ``APP_REPO_DIR`` pointing at somewhere that does not exist is a
    misconfiguration, and is raised rather than folded into the same None: a
    check that cannot run must not be indistinguishable from a check that
    passed. ``ops-server/CLAUDE.md`` states the rule for preflight — *skip is
    not pass* — and it holds here for the same reason.

    The sibling fallback is what breaks on the split: today Orbital sits two
    levels under ``enablement-framework/``, so ``../..`` finds the app repo
    beside the framework. From ``/home/ops/orbital`` the same walk reaches
    ``/home`` and finds nothing.
    """
    override = os.environ.get(APP_REPO_DIR_ENV)
    if override:
        path = Path(override)
        if not path.is_dir():
            raise FileNotFoundError(
                f"{APP_REPO_DIR_ENV}={override} does not exist — unset it to "
                "fall back to the sibling layout, or point it at the checkout"
            )
        return path
    sibling = framework_dir().parent / "dynatrace-app-enablements"
    return sibling if sibling.is_dir() else None
