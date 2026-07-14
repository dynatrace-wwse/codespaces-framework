"""Pure-logic tests for the fleet autoscaler (dashboard/fleet.py).

No AWS calls, no subprocess, no Redis — exercises only the pure helper
functions the async wrappers delegate to: the 4-instance safety cap,
tag-verification refusal for terminate, user-data generation, and the
expired-credentials error classification.

Runnable two ways:
  - pytest:     python3 -m pytest dashboard/test_fleet.py
                (/home/ops/ops-venv/bin/python -m pytest if pytest installed)
  - standalone: /home/ops/ops-venv/bin/python -m dashboard.test_fleet
"""

import base64

from dashboard import fleet


def _inst(instance_id="i-abc", **tags) -> dict:
    """Build a raw EC2 instance dict with the given tags."""
    return {
        "InstanceId": instance_id,
        "Tags": [{"Key": k.replace("_", "-"), "Value": v}
                 for k, v in tags.items()],
    }


# ── Safety cap ───────────────────────────────────────────────────────────────

def test_scale_count_within_cap_ok():
    for n in (1, 2, 3, 4):
        assert fleet._validate_scale_count(n) == n


def test_scale_count_over_cap_rejected():
    for n in (5, 10, 100):
        try:
            fleet._validate_scale_count(n)
        except ValueError as e:
            assert "safety limit" in str(e)
            assert str(fleet.MAX_SCALE_UP) in str(e)
        else:
            raise AssertionError(f"count={n} should have been rejected")


def test_scale_count_zero_negative_and_bool_rejected():
    for bad in (0, -1, True):
        try:
            fleet._validate_scale_count(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"count={bad!r} should have been rejected")


def test_cap_is_four():
    assert fleet.MAX_SCALE_UP == 4


# ── Tag-verification refusal (terminate) ─────────────────────────────────────

def test_verify_terminatable_role_tag_ok():
    ok, refused = fleet._verify_terminatable(
        [_inst("i-1", orbital_role="worker")])
    assert ok == ["i-1"] and refused == []


def test_verify_terminatable_spot_name_ok():
    ok, refused = fleet._verify_terminatable(
        [_inst("i-2", Name="orbital-worker-spot")])
    assert ok == ["i-2"] and refused == []


def test_verify_terminatable_refuses_master_and_untagged():
    descriptions = [
        _inst("i-master", Name="autonomous-enablements"),      # the master!
        _inst("i-pet", Name="autonomous-enablements-worker"),  # pet worker
        _inst("i-plain"),                                      # no tags
        _inst("i-wrongrole", orbital_role="master"),
        _inst("i-spot", Name="orbital-worker-spot"),           # allowed
    ]
    ok, refused = fleet._verify_terminatable(descriptions)
    assert ok == ["i-spot"]
    assert sorted(refused) == ["i-master", "i-pet", "i-plain", "i-wrongrole"]


def test_verify_terminatable_empty():
    assert fleet._verify_terminatable([]) == ([], [])


# ── Start/stop allow-list ────────────────────────────────────────────────────

def test_start_stop_allowed_worker_name_prefix():
    # worker-3 (i-03689a1374d39cb6a) style pet workers.
    assert fleet._start_stop_allowed(
        _inst(Name="autonomous-enablements-worker"))
    assert fleet._start_stop_allowed(
        _inst(Name="autonomous-enablements-worker-3"))


def test_start_stop_allowed_spot_worker():
    assert fleet._start_stop_allowed(_inst(Name="orbital-worker-spot"))
    assert fleet._start_stop_allowed(_inst(orbital_role="worker"))


def test_start_stop_refused_for_master_and_others():
    assert not fleet._start_stop_allowed(_inst(Name="autonomous-enablements"))
    assert not fleet._start_stop_allowed(_inst(Name="some-other-box"))
    assert not fleet._start_stop_allowed(_inst())


# ── User-data generation ─────────────────────────────────────────────────────

def test_user_data_worker_id_sed_and_append():
    script = fleet._build_user_data()
    # sed-replace of an existing WORKER_ID= line…
    assert "sed -i" in script
    assert "^WORKER_ID=" in script
    # …or append when absent.
    assert 'echo "WORKER_ID=${WORKER_ID}" >>' in script
    # Unique id derived from the instance id (last 8 chars).
    assert "worker-x86_64-spot-" in script
    assert "tail -c 8" in script


def test_user_data_uses_imdsv2_token():
    script = fleet._build_user_data()
    assert "http://169.254.169.254/latest/api/token" in script
    assert "X-aws-ec2-metadata-token-ttl-seconds" in script
    assert "X-aws-ec2-metadata-token:" in script
    assert "169.254.169.254/latest/meta-data/instance-id" in script


def test_user_data_master_redis_and_restart():
    script = fleet._build_user_data()
    assert fleet.MASTER_REDIS_HOST in script          # 172.31.36.172
    assert "MASTER_REDIS_URL" in script
    assert "systemctl restart ops-worker-agent" in script
    assert script.startswith("#!/bin/bash")


def test_user_data_encodes_to_base64_roundtrip():
    script = fleet._build_user_data()
    encoded = fleet._encode_user_data(script)
    assert base64.b64decode(encoded).decode() == script


# ── Expired-credentials classification ───────────────────────────────────────

def test_classify_expired_token():
    msg = fleet._classify_aws_error(
        "An error occurred (ExpiredToken) when calling the "
        "DescribeInstances operation: The security token included in "
        "the request is expired")
    assert msg == fleet.CREDS_ERROR
    assert "refresh ~/.aws/credentials" in msg


def test_classify_auth_failure_and_missing_creds():
    for stderr in (
        "An error occurred (AuthFailure) when calling the RunInstances "
        "operation: AWS was not able to validate the provided access "
        "credentials",
        "Unable to locate credentials. You can configure credentials by "
        "running \"aws configure\".",
        "An error occurred (RequestExpired) ...",
        "An error occurred (InvalidClientTokenId) ...",
    ):
        assert fleet._classify_aws_error(stderr) == fleet.CREDS_ERROR


def test_classify_other_errors_pass_through():
    stderr = ("An error occurred (InvalidInstanceID.NotFound) when calling "
              "the TerminateInstances operation: The instance ID "
              "'i-deadbeef' does not exist")
    msg = fleet._classify_aws_error(stderr)
    assert msg == stderr.strip()
    assert msg != fleet.CREDS_ERROR


def test_classify_empty_stderr():
    assert "aws CLI failed" in fleet._classify_aws_error("")


# ── Instance parsing (list_fleet shape) ──────────────────────────────────────

def test_parse_instances_shape_and_lifecycle():
    reservations = [{
        "Instances": [
            {
                "InstanceId": "i-spot1",
                "InstanceType": "c5.2xlarge",
                "State": {"Name": "running"},
                "PrivateIpAddress": "172.31.40.1",
                "InstanceLifecycle": "spot",
                "LaunchTime": "2026-07-14T10:00:00+00:00",
                "Tags": [{"Key": "Name", "Value": "orbital-worker-spot"}],
            },
            {
                "InstanceId": "i-master",
                "InstanceType": "c5.4xlarge",
                "State": {"Name": "running"},
                "PrivateIpAddress": "172.31.36.172",
                "LaunchTime": "2026-01-01T00:00:00+00:00",
                "Tags": [{"Key": "Name", "Value": "autonomous-enablements"}],
            },
        ],
    }]
    recs = fleet._parse_instances(reservations)
    assert len(recs) == 2
    spot = next(r for r in recs if r["instance_id"] == "i-spot1")
    master = next(r for r in recs if r["instance_id"] == "i-master")
    assert spot["lifecycle"] == "spot"
    assert master["lifecycle"] == "on-demand"   # no InstanceLifecycle key
    assert spot["name"] == "orbital-worker-spot"
    assert spot["private_ip"] == "172.31.40.1"
    assert master["type"] == "c5.4xlarge"
    assert set(spot) == {"instance_id", "name", "type", "state",
                         "private_ip", "lifecycle", "launch_time"}


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print("all fleet tests passed")
