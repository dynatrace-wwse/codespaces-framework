"""Tests for executor._write_env_file multi-tenancy DT-credential selection.

Hard rule: never write CoE tokens into a non-CoE tenant's daemon .env.

Run: /home/ops/ops-venv/bin/python -m worker-agent.test_executor_env
"""

import tempfile
from pathlib import Path

from . import executor as ex

COE = "https://geu80787.apps.dynatrace.com"
OTHER = "https://sro97894.apps.dynatrace.com"


def _write(**kw) -> dict:
    ex.DT_ENVIRONMENT = COE
    ex.DT_OPERATOR_TOKEN = "COE-STATIC-OPERATOR-VALUE"
    ex.DT_INGEST_TOKEN = "COE-STATIC-INGEST-VALUE"
    ex.DT_LLM_TOKEN = "COE-STATIC-LLM-VALUE"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        ex._write_env_file(p, **kw)
        out = {}
        for line in p.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out


def test_coe_uses_static():
    env = _write()  # integration test: no tenant, no overrides
    assert env["DT_OPERATOR_TOKEN"] == "COE-STATIC-OPERATOR-VALUE"
    assert env["DT_ENVIRONMENT"] == COE


def test_minted_overrides_used_no_coe_leak():
    minted = {
        "DT_ENVIRONMENT": OTHER,
        "DT_OPERATOR_TOKEN": "MINTED-OP",
        "DT_INGEST_TOKEN": "MINTED-IN",
    }
    env = _write(overrides=minted, tenant=OTHER)
    assert env["DT_OPERATOR_TOKEN"] == "MINTED-OP"
    assert env["DT_ENVIRONMENT"] == OTHER
    # No CoE static must leak in — not even DT_LLM_TOKEN.
    assert "STATIC" not in env.get("DT_OPERATOR_TOKEN", "")
    assert "DT_LLM_TOKEN" not in env


def test_non_coe_without_minted_fails_closed():
    env = _write(tenant=OTHER)
    assert env["DT_ENVIRONMENT"] == OTHER
    assert "DT_OPERATOR_TOKEN" not in env
    assert "DT_INGEST_TOKEN" not in env
    assert "DT_LLM_TOKEN" not in env


def test_hostgroup_written_for_user():
    from datetime import datetime, timezone
    env = _write(user="TestUser")
    assert env["DT_HOSTGROUP"] == f"testuser-{datetime.now(timezone.utc):%Y%m%d}"


def test_hostgroup_email_keeps_local_part_sanitized():
    from datetime import datetime, timezone
    env = _write(user="Sergio.Hinojosa@dynatrace.com")
    assert env["DT_HOSTGROUP"] == f"sergio-hinojosa-{datetime.now(timezone.utc):%Y%m%d}"


def test_hostgroup_omitted_without_user():
    env = _write()
    assert "DT_HOSTGROUP" not in env


def test_hostgroup_explicit_wins_over_derived():
    env = _write(hostgroup="alice-20260101", user="bob@example.com")
    assert env["DT_HOSTGROUP"] == "alice-20260101"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all executor-env tests passed")
