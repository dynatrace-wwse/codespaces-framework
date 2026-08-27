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
    # The environment tag is conditioned on the same way — see
    # docs/iam/environment-isolation.md. Renaming it here without updating both
    # role policies makes every launch fail, and every terminate succeed
    # against the wrong environment.
    assert fleet.ENV_TAG_KEY == "env"
    from shared import environment
    assert environment.ENV_TAG_KEY == fleet.ENV_TAG_KEY
    assert set(environment.KNOWN_ENVS) == {"prod", "staging"}


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


# ── IMDS reachability from a learner's container ─────────────────────────────

def test_launch_pins_the_imds_hop_limit_to_one():
    """A learner's container reaches IMDS at hop 2, and cannot at hop 1.

    Measured on a live slot rather than reasoned about: IMDSv1 GET from inside
    sb-slot-arm518-0 returned `401 Unauthorized`, i.e. the packet arrived and
    only the token was missing. The route out is via the docker bridge, one hop.
    At 2 the learner is one PUT away from the host's instance-profile
    credentials and from anything user-data carries; at 1 the request dies
    before it leaves the box while the host, being hop 0, is unaffected.
    """
    opts = fleet._metadata_options()
    assert f"HttpPutResponseHopLimit={fleet.WORKER_IMDS_HOP_LIMIT}" in opts
    assert fleet.WORKER_IMDS_HOP_LIMIT == 1
    # IMDSv2-only. A launch born on v1 needs no token at all, which puts the
    # hop limit back to being the only thing standing in the way.
    assert "HttpTokens=required" in opts
    assert "HttpEndpoint=enabled" in opts


def test_launch_actually_passes_the_metadata_options():
    """The constant is worthless if RunInstances never sees it.

    Omitting the flag does not fail the launch — it inherits the account default
    of hop limit 2 and the worker comes up looking perfectly healthy, which is
    precisely why this needs a test rather than a code review.
    """
    import inspect
    src = inspect.getsource(fleet.scale_up)
    assert "--metadata-options" in src
    assert "_metadata_options()" in src


# ── The checkout path a worker syncs is configuration, not a literal ─────────

def test_user_data_checkout_defaults_to_todays_path(monkeypatch):
    """Unset means nothing moves. The repo split flips one variable."""
    monkeypatch.delenv("ORBITAL_CHECKOUT", raising=False)
    assert "CHECKOUT=/home/ops/enablement-framework/codespaces-framework" in \
        fleet._build_user_data()


def test_user_data_checkout_follows_orbital_checkout(monkeypatch):
    """After the split Orbital is its own repo at its own path.

    The sync in this script is best-effort by design — a worker whose `git -C
    $CHECKOUT` fails still boots and still takes jobs, running whatever code the
    AMI was baked with, and says so only in a log file. So a stale literal here
    does not announce itself: it produces a worker that looks healthy and serves
    learners old code. See feedback_fleet_code_sync_invariant.
    """
    monkeypatch.setenv("ORBITAL_CHECKOUT", "/home/ops/orbital")
    script = fleet._build_user_data()
    assert "CHECKOUT=/home/ops/orbital" in script
    assert "enablement-framework" not in script


def test_user_data_checkout_is_not_resolved_against_the_callers_home(monkeypatch, tmp_path):
    """This script runs as root on a worker, not as whoever generated it."""
    monkeypatch.delenv("ORBITAL_CHECKOUT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert str(tmp_path) not in fleet._build_user_data()


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
                         "private_ip", "lifecycle", "launch_time", "pool", "env"}
    # The lane must survive the flattening. Dropping every tag here is what left
    # the UI unable to tell a workshop machine from a self-service one, and it
    # is the same blind spot that let a scale-down click cordon a workshop box.
    assert spot["pool"] == "ws-abc123"
    # An untagged instance (the long-lived pet workers) reports no lane rather
    # than a guessed one; consumers read empty as daily, as they do everywhere.
    assert master["pool"] == ""


# ── Environment isolation ────────────────────────────────────────────────────
#
# Staging and production share one AWS account. These are the tests that fail
# if the environment scope is ever dropped from a guard — which is the single
# most likely way this design gets broken, because dropping it makes nothing
# fail visibly until the day one environment reaps the other's fleet.

import os
from contextlib import contextmanager

from shared import environment


@contextmanager
def _as_env(name):
    prev = os.environ.get("ORBITAL_ENV")
    os.environ["ORBITAL_ENV"] = name
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("ORBITAL_ENV", None)
        else:
            os.environ["ORBITAL_ENV"] = prev


def test_launched_instance_carries_its_environment_tag():
    """Every launch is tagged with the launching environment.

    An untagged launch is a machine this environment can create but can never
    afterwards terminate, because the IAM condition will not match it — it
    would run to its in-instance shutdown backstop with nothing able to reap it.

    Asserted on the source of scale_up, matching how the pool tag is pinned:
    the tag string is built inline and the alternative is mocking the AWS CLI.
    """
    import inspect
    src = inspect.getsource(fleet.scale_up)
    assert f"Key={{ENV_TAG_KEY}},Value=" in src or "Key={ENV_TAG_KEY},Value=" in src
    assert fleet.ENV_TAG_KEY == "env"


def test_prod_refuses_to_terminate_a_staging_worker():
    staging_worker = _inst("i-staging", orbital_role="worker", env="staging")
    with _as_env("prod"):
        ok, refused = fleet._verify_terminatable([staging_worker])
    assert ok == []
    assert refused == ["i-staging"]


def test_staging_refuses_to_terminate_a_prod_worker():
    prod_worker = _inst("i-prod", orbital_role="worker", env="prod")
    with _as_env("staging"):
        ok, refused = fleet._verify_terminatable([prod_worker])
    assert ok == []
    assert refused == ["i-prod"]


def test_staging_refuses_to_terminate_an_UNTAGGED_worker():
    # The four long-lived production machines carry no env tag. Staging must
    # treat them as production's, not as unclaimed.
    legacy = _inst("i-legacy", orbital_role="worker")
    with _as_env("staging"):
        ok, refused = fleet._verify_terminatable([legacy])
    assert ok == []
    assert refused == ["i-legacy"]


def test_each_environment_still_terminates_its_OWN_worker():
    # The isolation must not be so broad that it breaks the actual job. If this
    # ever fails, autoscaling is dead in that environment.
    with _as_env("prod"):
        ok, refused = fleet._verify_terminatable(
            [_inst("i-p", orbital_role="worker", env="prod")])
        assert (ok, refused) == (["i-p"], [])
        # ...including the untagged legacy machines, pre-backfill.
        ok, refused = fleet._verify_terminatable(
            [_inst("i-old", orbital_role="worker")])
        assert (ok, refused) == (["i-old"], [])
    with _as_env("staging"):
        ok, refused = fleet._verify_terminatable(
            [_inst("i-s", orbital_role="worker", env="staging")])
        assert (ok, refused) == (["i-s"], [])


def test_terminate_isolation_holds_in_both_directions_for_every_pair():
    # Stated as a property rather than two examples: for every (caller, owner)
    # pair, terminate is permitted if and only if they are the same
    # environment. Deleting the env check from _is_spot_worker makes the
    # off-diagonal cases fail here.
    for caller in environment.KNOWN_ENVS:
        for owner in environment.KNOWN_ENVS:
            inst = _inst("i-x", orbital_role="worker", env=owner)
            with _as_env(caller):
                ok, _ = fleet._verify_terminatable([inst])
            assert bool(ok) == (caller == owner), \
                f"caller={caller} owner={owner} -> ok={ok}"


def test_start_stop_isolation_holds_in_both_directions():
    # Stopping another environment's machine is quieter than terminating it and
    # just as wrong: it removes capacity from a fleet that is not ours and
    # looks like a spot interruption to whoever owns it.
    for caller in environment.KNOWN_ENVS:
        for owner in environment.KNOWN_ENVS:
            pet = _inst("i-pet", Name="autonomous-enablements-worker-3", env=owner)
            with _as_env(caller):
                allowed = fleet._start_stop_allowed(pet)
            assert allowed == (caller == owner), \
                f"caller={caller} owner={owner} -> {allowed}"


def test_parsed_records_report_their_environment():
    recs = fleet._parse_instances([{"Instances": [
        {"InstanceId": "i-s", "Tags": [{"Key": "env", "Value": "staging"}]},
        {"InstanceId": "i-legacy", "Tags": []},
    ]}])
    by_id = {r["instance_id"]: r for r in recs}
    assert by_id["i-s"]["env"] == "staging"
    # Untagged surfaces as prod, not as blank — a blank would render in the
    # fleet table as an unowned machine that anyone may scale down.
    assert by_id["i-legacy"]["env"] == "prod"


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
