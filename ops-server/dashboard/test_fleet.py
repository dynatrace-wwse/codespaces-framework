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
import json

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


def test_user_data_master_redis_and_agent_lifecycle():
    script = fleet._build_user_data()
    assert fleet.MASTER_REDIS_HOST in script          # 172.31.36.172
    assert "MASTER_REDIS_URL" in script
    assert script.startswith("#!/bin/bash")
    # The agent must be STOPPED before its identity is rewritten and STARTED
    # after -- a plain `restart` at the end would let the AMI-inherited
    # identity heartbeat (as the worker the image was baked from) in the
    # window before the rewrite lands.
    stop_at = script.index("systemctl stop ops-worker-agent")
    start_at = script.index("systemctl start ops-worker-agent")
    worker_id_at = script.index("WORKER_ID=")
    assert stop_at < worker_id_at < start_at


def test_user_data_lifetime_arms_self_termination():
    """A scheduled-lifetime worker must be able to kill itself with no help."""
    armed = fleet._build_user_data(lifetime_minutes=1440)
    assert "shutdown -h +1440" in armed
    # Default must NOT arm a self-destruct -- a pet worker that silently
    # halted itself would be a far worse bug than one that outlives its window.
    assert "shutdown -h" not in fleet._build_user_data()


def test_user_data_capacity_override():
    assert "WORKER_CAPACITY=30" in fleet._build_user_data(capacity=30)


def test_user_data_syncs_code_before_starting_the_agent():
    """The golden AMI is a snapshot; an unsynced worker serves learners with
    whatever agent code existed the day the image was baked.

    Ordering is the substance of this test: the sync has to complete before
    ``systemctl start ops-worker-agent``, or the agent loads the stale modules
    and the pull only takes effect on the next restart -- which never comes.
    """
    script = fleet._build_user_data()
    assert 'git -C "$CHECKOUT" fetch --quiet origin main' in script
    sync_at = script.index("reset --hard --quiet FETCH_HEAD")
    start_at = script.index("systemctl start ops-worker-agent")
    assert sync_at < start_at, "code sync must run BEFORE the agent starts"


def test_user_data_leaves_code_ref_empty_when_sync_fails():
    """A failed sync must not stamp a reassuring value.

    WORKER_CODE_REF is the only signal the master has that a worker is running
    current code. Defaulting it to anything on failure would turn a visible
    stale worker into an invisible one -- the exact failure mode this field was
    added to expose.
    """
    script = fleet._build_user_data()
    assert 'CODE_REF=""' in script
    assert "WARNING code sync FAILED" in script
    # The stamp is written unconditionally from CODE_REF, so an empty ref
    # reaches .env rather than being skipped (skipping would leave the AMI's
    # previous value in place, which is worse than empty: it would be a LIE).
    assert "WORKER_CODE_REF=${CODE_REF}" in script


def test_user_data_sets_pool_and_defaults_to_daily():
    assert "WORKER_POOL=daily" in fleet._build_user_data()
    assert "WORKER_POOL=ws_bootcamp" in fleet._build_user_data(pool="ws_bootcamp")


def test_launched_instance_is_tagged_with_its_pool():
    """The pool is tagged as well as written to .env.

    user-data runs once, on first boot -- so after a stop/start the tag is the
    only surviving record of which workshop a machine belongs to, and it is
    what lets the reaper clean up a finished workshop without Redis.
    """
    import inspect
    src = inspect.getsource(fleet.scale_up)
    assert "Key=orbital-pool,Value=" in src


def test_fleet_tag_constants_match_iam_policy():
    """These exact strings are what the OrbitalFleetAutoscaler IAM policy
    conditions on. If they drift, every launch fails UnauthorizedOperation."""
    assert fleet.FLEET_TAG_KEY == "ManagedBy"
    assert fleet.FLEET_TAG_VALUE == "orbital-autoscaler"


# ── Root volume override ─────────────────────────────────────────────────────

def test_root_block_device_raises_throughput_above_ami_baseline():
    """The AMI bakes gp3 at the free 125 MiB/s baseline, and disk throughput is
    the measured binding ceiling on simultaneous lab installs. A worker born at
    125 delivers ~18 seats where the picker promises 30."""
    bdm = json.loads(fleet._root_block_device())
    assert len(bdm) == 1
    ebs = bdm[0]["Ebs"]
    assert bdm[0]["DeviceName"] == fleet.WORKER_ROOT_DEVICE
    assert ebs["VolumeType"] == "gp3"
    assert ebs["Throughput"] == fleet.WORKER_ROOT_THROUGHPUT_MBPS > 125


def test_root_block_device_iops_can_sustain_the_throughput():
    """gp3 caps throughput at 0.25 MiB/s per provisioned IOPS. Asking for more
    than the IOPS can carry is rejected by RunInstances at launch."""
    ebs = json.loads(fleet._root_block_device())[0]["Ebs"]
    assert ebs["Throughput"] <= ebs["Iops"] * 0.25
    assert ebs["Throughput"] <= 1000        # gp3 hard maximum


def test_launched_volume_is_what_the_planner_assumed_it_would_be():
    """The planner sizes a class from a volume it never sees. If the launcher
    and the planner hold two copies of these numbers, the fleet is silently
    oversold by exactly the gap — which is the shape of the 18-vs-30 bug the
    IOPS term was added to explain. One source, asserted end to end.
    """
    from dashboard import fleet_policy
    ebs = json.loads(fleet._root_block_device())[0]["Ebs"]
    assert ebs["Iops"] == fleet_policy.FLEET_VOLUME_IOPS
    assert ebs["Throughput"] == fleet_policy.FLEET_VOLUME_THROUGHPUT_MBPS
    # And the point of provisioning them: neither disk dimension may be the
    # ceiling on the largest shape we buy, so capacity is decided by memory.
    assert fleet_policy.iops_slots(ebs["Iops"]) > fleet_policy.memory_slots("m6a.4xlarge")
    assert fleet_policy.disk_slots(ebs["Throughput"]) > fleet_policy.memory_slots("m6a.4xlarge")
    assert fleet_policy.limiting_factor("m6a.4xlarge") == "memory"


def test_root_block_device_deletes_on_termination():
    """A terminated spot worker must not leave a 300 GiB volume we still pay for."""
    ebs = json.loads(fleet._root_block_device())[0]["Ebs"]
    assert ebs["DeleteOnTermination"] is True
    assert ebs["VolumeSize"] == fleet.WORKER_ROOT_SIZE_GB


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
                "Tags": [{"Key": "Name", "Value": "orbital-worker-spot"},
                         {"Key": "orbital-pool", "Value": "ws-abc123"}],
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
                         "private_ip", "lifecycle", "launch_time", "pool"}
    # The lane must survive the flattening. Dropping every tag here is what left
    # the UI unable to tell a workshop machine from a self-service one, and it
    # is the same blind spot that let a scale-down click cordon a workshop box.
    assert spot["pool"] == "ws-abc123"
    # An untagged instance (the long-lived pet workers) reports no lane rather
    # than a guessed one; consumers read empty as daily, as they do everywhere.
    assert master["pool"] == ""


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
