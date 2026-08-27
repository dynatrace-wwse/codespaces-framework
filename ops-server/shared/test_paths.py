"""Tests for shared/paths.py — the resolution the repo split turns on."""

from pathlib import Path

import pytest

from shared import paths


# ── Defaults must not move anything ──────────────────────────────────────────

def test_defaults_are_todays_layout(monkeypatch):
    """Importing this module changes no path. The split is a config change.

    If a default drifts from the live layout, the failure is not an import
    error — it is a worker that boots, syncs nothing, and serves learners
    whatever code its AMI was baked with.
    """
    monkeypatch.delenv(paths.ORBITAL_CHECKOUT_ENV, raising=False)
    monkeypatch.delenv(paths.FRAMEWORK_DIR_ENV, raising=False)
    assert paths.fleet_host_orbital_checkout() == (
        "/home/ops/enablement-framework/codespaces-framework"
    )
    assert paths.framework_dir() == (
        Path.home() / "enablement-framework" / "codespaces-framework"
    )


def test_env_overrides_win(monkeypatch):
    monkeypatch.setenv(paths.ORBITAL_CHECKOUT_ENV, "/home/ops/orbital")
    monkeypatch.setenv(paths.FRAMEWORK_DIR_ENV, "/home/ops/framework")
    assert paths.fleet_host_orbital_checkout() == "/home/ops/orbital"
    assert paths.framework_dir() == Path("/home/ops/framework")


def test_empty_env_falls_back_rather_than_returning_nothing(monkeypatch):
    """An unset variable in systemd arrives as the empty string, not as absent.

    Treating "" as an override puts an empty path into a worker's boot script,
    where `git -C "" fetch` fails in a way the best-effort sync swallows.
    """
    monkeypatch.setenv(paths.ORBITAL_CHECKOUT_ENV, "")
    monkeypatch.setenv(paths.FRAMEWORK_DIR_ENV, "")
    assert paths.fleet_host_orbital_checkout() == (
        "/home/ops/enablement-framework/codespaces-framework"
    )
    assert paths.framework_dir() == (
        Path.home() / "enablement-framework" / "codespaces-framework"
    )


# ── The fleet-host path must not be resolved against the caller's home ───────

def test_fleet_host_checkout_ignores_the_callers_home(monkeypatch, tmp_path):
    """It describes a path on ANOTHER machine, written into a root-run script.

    The dashboard runs as `ops`, so a Path.home() rooting looks correct in
    production and silently bakes /home/<developer> into a worker's user-data
    when the same function is called from a dev shell.
    """
    monkeypatch.delenv(paths.ORBITAL_CHECKOUT_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.fleet_host_orbital_checkout().startswith("/home/ops/")
    assert str(tmp_path) not in paths.fleet_host_orbital_checkout()


# ── app_repo_dir: absent is fine, misconfigured is not ───────────────────────

def test_app_repo_dir_returns_none_when_genuinely_absent(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.APP_REPO_DIR_ENV, raising=False)
    monkeypatch.setenv(paths.FRAMEWORK_DIR_ENV, str(tmp_path / "framework"))
    assert paths.app_repo_dir() is None


def test_app_repo_dir_finds_the_sibling_checkout(monkeypatch, tmp_path):
    (tmp_path / "dynatrace-app-enablements").mkdir()
    monkeypatch.delenv(paths.APP_REPO_DIR_ENV, raising=False)
    monkeypatch.setenv(paths.FRAMEWORK_DIR_ENV, str(tmp_path / "codespaces-framework"))
    assert paths.app_repo_dir() == tmp_path / "dynatrace-app-enablements"


def test_app_repo_dir_raises_on_a_path_that_is_not_there(monkeypatch, tmp_path):
    """Skip is not pass. A configured-but-wrong path is a misconfiguration,
    and must not be indistinguishable from 'this checkout does not have it'."""
    monkeypatch.setenv(paths.APP_REPO_DIR_ENV, str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError):
        paths.app_repo_dir()
