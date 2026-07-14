"""Tests for _write_env_file multi-tenancy DT-credential selection.

Hard rule: never write CoE tokens into a non-CoE tenant's job .env.

Run: /home/ops/ops-venv/bin/python -m workers.test_manager_env
  or pytest workers/test_manager_env.py
"""

from pathlib import Path

import workers.manager as m

COE = "https://geu80787.apps.dynatrace.com"


def _write(tmp: Path, **kw) -> dict:
    """Call _write_env_file on a bare instance (it uses no instance state) and
    parse the resulting .env into a dict."""
    inst = m.WorkerManager.__new__(m.WorkerManager)
    env_path = tmp / ".devcontainer" / ".env"
    # Patch module credential globals to known values for the assertions.
    m.DT_ENVIRONMENT = COE
    m.DT_OPERATOR_TOKEN = "COE-STATIC-OPERATOR-VALUE"
    m.DT_INGEST_TOKEN = "COE-STATIC-INGEST-VALUE"
    inst._write_env_file(env_path, arch="arm64", job_id="job-1", **kw)
    out = {}
    for line in env_path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_coe_tenant_uses_static(tmp_path):
    # No tenant (internal framework test) → static CoE creds are fine.
    env = _write(tmp_path)
    assert env["DT_OPERATOR_TOKEN"] == "COE-STATIC-OPERATOR-VALUE"
    assert env["DT_INGEST_TOKEN"] == "COE-STATIC-INGEST-VALUE"
    assert env["DT_ENVIRONMENT"] == COE


def test_coe_url_tenant_uses_static(tmp_path):
    # Tenant explicitly == CoE → static is correct.
    env = _write(tmp_path, tenant=COE)
    assert env["DT_OPERATOR_TOKEN"] == "COE-STATIC-OPERATOR-VALUE"


def test_minted_tokens_used_for_non_coe(tmp_path):
    other = "https://sro97894.apps.dynatrace.com"
    minted = {
        "DT_ENVIRONMENT": other,
        "DT_OPERATOR_TOKEN": "MINTED-OPERATOR-VALUE",
        "DT_INGEST_TOKEN": "MINTED-INGEST-VALUE",
        "DT_ONEAGENT_TOKEN": "MINTED-ONEAGENT-VALUE",
    }
    env = _write(tmp_path, dt_env=minted, tenant=other)
    assert env["DT_OPERATOR_TOKEN"] == "MINTED-OPERATOR-VALUE"
    assert env["DT_INGEST_TOKEN"] == "MINTED-INGEST-VALUE"
    assert env["DT_ONEAGENT_TOKEN"] == "MINTED-ONEAGENT-VALUE"
    assert env["DT_ENVIRONMENT"] == other
    # CoE statics must NOT leak in.
    assert "STATIC" not in env["DT_OPERATOR_TOKEN"]
    assert "STATIC" not in env["DT_INGEST_TOKEN"]


def test_non_coe_without_minted_fails_closed(tmp_path):
    # Non-CoE tenant, no minted tokens → write tenant URL but NO tokens (never CoE).
    other = "https://sro97894.apps.dynatrace.com"
    env = _write(tmp_path, tenant=other)
    assert env["DT_ENVIRONMENT"] == other
    assert env["DT_OPERATOR_TOKEN"] == ""
    assert env["DT_INGEST_TOKEN"] == ""
    assert "DT_ONEAGENT_TOKEN" not in env  # omitted when unset


def test_hostgroup_written_for_user(tmp_path):
    from datetime import datetime, timezone
    env = _write(tmp_path, user="Sergio.Hinojosa@dynatrace.com")
    assert env["DT_HOSTGROUP"] == f"sergio-hinojosa-{datetime.now(timezone.utc):%Y%m%d}"


def test_hostgroup_omitted_without_user(tmp_path):
    env = _write(tmp_path)
    assert "DT_HOSTGROUP" not in env


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
            print(f"ok {name}")
    print("all manager-env tests passed")
