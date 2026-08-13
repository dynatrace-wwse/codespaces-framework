"""Fleet — EC2 spot-worker autoscaling for the Orbital worker pool.

AWS access goes through the aws CLI v2 (``/usr/local/bin/aws``) via
``asyncio.create_subprocess_exec`` — boto3 is deliberately NOT a dependency.
Credentials come from the service user's ``~/.aws/credentials`` (federated
STS — they EXPIRE). When the CLI fails with an auth/expiry error we surface
one clear, stable message: :data:`CREDS_ERROR`.

Design: every AWS-touching coroutine delegates its decision logic to small
pure functions (``_build_user_data``, ``_classify_aws_error``,
``_verify_terminatable``, ``_validate_scale_count``, ``_start_stop_allowed``,
``_parse_instances``) so the safety rules are unit-testable without any
subprocess or network (see ``dashboard/test_fleet.py``).
"""

import asyncio
import base64
import json

AWS_CLI = "/usr/local/bin/aws"
# Home region: where the master, Redis and the golden AMI live. Every call
# accepts an explicit region so a bootcamp can be run elsewhere (ap-southeast-1
# for the APAC event) — but anything that resolves networking from the template
# instance is home-region only until the AMI is copied. See region_ready().
REGION = "eu-west-2"

# AMI per region. A launch in a region with no entry must FAIL LOUDLY rather
# than fall back to the home AMI: an AMI id is region-scoped, so a fallback
# would launch nothing and report success.
WORKER_AMI_BY_REGION = {
    "eu-west-2": "ami-01c331ae9b0054602",
}
# Template instance per region — its subnet / security groups / key-name are
# resolved live at launch so networking never drifts from production.
TEMPLATE_INSTANCE_BY_REGION = {
    "eu-west-2": "i-02b773319c758fe40",
}
# Redis endpoint a worker in this region should phone home to. Cross-region
# workers need VPC peering — never expose Redis to the internet.
MASTER_REDIS_BY_REGION = {
    "eu-west-2": "172.31.36.172",
}

# Golden worker AMI v2 — baked 2026-07-15 from the LIVE worker-1 (docker +
# sysbox + ops-worker-agent + current /home/ops/.env). v1 (ami-0ed76cf85fa7d2967,
# from stopped worker-3) was bare Ubuntu — spot workers never registered.
# NOTE: the baked agent boots as amd001 until cloud-init's user-data rewrites
# WORKER_ID and restarts it — a few seconds of duplicate registration that the
# real amd001's next heartbeat overwrites.
WORKER_AMI = "ami-01c331ae9b0054602"
# The AMI bakes a 300 GiB gp3 root at the FREE BASELINE 125 MiB/s, and disk
# throughput is the measured binding ceiling on a lab install (~3.75 GB of
# pull+extract per session): at 125 MiB/s an m6a.4xlarge tops out at ~18
# simultaneous installs, at 500 it is memory-bound at 30. Launching from the
# AMI unmodified therefore births every autoscaled worker at 60% of the
# capacity the picker promises for it. Override it at RunInstances — this
# needs no IAM change (the role's RunInstances already covers volume/*), and
# it is strictly better than ModifyVolume-after-launch, which would race the
# worker registering itself as ready with its full nominal capacity.
WORKER_ROOT_DEVICE = "/dev/sda1"
WORKER_ROOT_SIZE_GB = 300
WORKER_ROOT_THROUGHPUT_MBPS = 500    # gp3 max 1000; needs >=2000 IOPS (0.25 MiB/s per IOPS)
WORKER_ROOT_IOPS = 3000
# worker-1 — template instance whose subnet / security groups / key-name are
# resolved dynamically at scale-up time so networking never drifts from prod.
TEMPLATE_INSTANCE_ID = "i-02b773319c758fe40"
# Master's private IP — spot workers must point MASTER_REDIS_URL here.
MASTER_REDIS_HOST = "172.31.36.172"

PROJECT_TAG = "autonomous-enablements"
SPOT_WORKER_NAME = "orbital-worker-spot"
WORKER_ROLE_TAG = "worker"           # value of the orbital-role tag

# The IAM boundary. The master runs under the OrbitalFleetAutoscaler instance
# profile, whose policy scopes every mutating EC2 action to instances carrying
# this tag. Changing either half without the other breaks autoscaling; dropping
# it entirely would widen the role's blast radius to the whole account.
FLEET_TAG_KEY = "ManagedBy"
FLEET_TAG_VALUE = "orbital-autoscaler"
WORKER_NAME_PREFIX = "autonomous-enablements-worker"

# Hard safety limit: never launch more than this many instances per call.
MAX_SCALE_UP = 4

DEFAULT_INSTANCE_TYPE = "c5.2xlarge"

# vCPU service quota codes. On-Demand and Spot are SEPARATE quotas, per region —
# a fleet that fits one can be refused by the other, and a VcpuLimitExceeded
# mid-herd looks exactly like a boot failure.
QUOTA_ONDEMAND_STANDARD = "L-1216C47A"
QUOTA_SPOT_STANDARD = "L-34B43A08"

CREDS_ERROR = (
    "AWS credentials expired or missing — refresh ~/.aws/credentials"
)

_CRED_ERROR_MARKERS = (
    "ExpiredToken",
    "RequestExpired",
    "AuthFailure",
    "InvalidClientTokenId",
    "Unable to locate credentials",
)


class FleetError(RuntimeError):
    """AWS CLI failure (including expired federated credentials)."""


# ── Pure helpers (unit-tested, no subprocess / no AWS) ───────────────────────

def _classify_aws_error(stderr: str) -> str:
    """Map raw ``aws`` CLI stderr to a user-facing error string.

    Federated STS creds in ~/.aws/credentials expire; the CLI then fails with
    ExpiredToken / AuthFailure / "Unable to locate credentials". Surface one
    clear message for all of those; pass anything else through (trimmed).
    """
    text = (stderr or "").strip()
    if any(marker in text for marker in _CRED_ERROR_MARKERS):
        return CREDS_ERROR
    return text or "aws CLI failed with no error output"


def _validate_scale_count(count: int) -> int:
    """Enforce the hard scale-up safety cap. Returns the validated count."""
    if not isinstance(count, int) or isinstance(count, bool):
        raise ValueError("count must be an integer")
    if count < 1:
        raise ValueError("count must be >= 1")
    if count > MAX_SCALE_UP:
        raise ValueError(
            f"count {count} exceeds the hard safety limit of "
            f"{MAX_SCALE_UP} instances per scale-up"
        )
    return count


def _build_user_data(redis_host: str = MASTER_REDIS_HOST,
                     capacity: int | None = None,
                     lifetime_minutes: int = 0) -> str:
    """Cloud-init user-data shell script for a fresh worker.

    - Stops the baked agent FIRST. The golden AMI was baked from a live worker,
      so it boots carrying that worker's identity and would heartbeat as it
      (overwriting the real worker's registration) until this script lands.
    - Derives a unique WORKER_ID from the instance id (IMDSv2, token-based).
    - Clears WORKER_SSH_HOST, which the AMI inherited from the instance it was
      baked from. Left stale, the master's PTY bridge SSHes to the WRONG BOX for
      every shell opened on this worker — observed on the first launched worker
      (2026-08-12): it registered ssh_host=autonomous-enablements-worker.
    - Ensures MASTER_REDIS_URL points at this region's master (sed-rewrites the
      host part of an existing redis:// URL — password userinfo preserved).
    - Optionally pins WORKER_CAPACITY, so a bigger instance type warms the slot
      count its memory actually supports instead of the AMI's baked-in figure.
    - Starts ops-worker-agent so it registers with the correct identity.
    """
    lifetime_block = ""
    if lifetime_minutes > 0:
        # `shutdown -h +N` is a kernel-level timer armed at boot. Once set it
        # needs nothing from us: not Orbital, not Redis, not a valid AWS
        # credential, not even a working network. Paired with
        # --instance-initiated-shutdown-behavior terminate, the instance
        # disappears at the deadline whatever else has failed.
        lifetime_block = f"""
# Self-destruct: hard stop after {lifetime_minutes} minutes, no external dependency.
shutdown -h +{lifetime_minutes} "orbital: scheduled fleet lifetime reached" || true
echo "orbital: self-terminate armed for +{lifetime_minutes}m" >> /var/log/orbital-worker-init.log
"""
    capacity_block = ""
    if capacity:
        capacity_block = f"""
# Slot count for this instance size (overrides whatever the AMI baked in).
if grep -q '^WORKER_CAPACITY=' "$ENV_FILE"; then
  sed -i "s|^WORKER_CAPACITY=.*|WORKER_CAPACITY={capacity}|" "$ENV_FILE"
else
  echo "WORKER_CAPACITY={capacity}" >> "$ENV_FILE"
fi
"""
    return f"""#!/bin/bash
set -uo pipefail
ENV_FILE=/home/ops/.env

# Stop BEFORE rewriting identity: the golden AMI boots as the worker it was
# baked from and would briefly heartbeat under that worker's id.
systemctl stop ops-worker-agent || true

# IMDSv2: fetch a session token, then the instance id (last 8 chars).
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \\
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \\
  "http://169.254.169.254/latest/meta-data/instance-id" | tail -c 8)
PRIVATE_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \\
  "http://169.254.169.254/latest/meta-data/local-ipv4")
WORKER_ID="worker-x86_64-spot-${{INSTANCE_ID}}"

touch "$ENV_FILE"

# Set the unique WORKER_ID (replace existing line or append).
if grep -q '^WORKER_ID=' "$ENV_FILE"; then
  sed -i "s|^WORKER_ID=.*|WORKER_ID=${{WORKER_ID}}|" "$ENV_FILE"
else
  echo "WORKER_ID=${{WORKER_ID}}" >> "$ENV_FILE"
fi

# Point WORKER_SSH_HOST at THIS box. Inherited from the AMI it names the
# instance the image was baked from, and the master would SSH there for every
# shell session opened against this worker.
if grep -q '^WORKER_SSH_HOST=' "$ENV_FILE"; then
  sed -i "s|^WORKER_SSH_HOST=.*|WORKER_SSH_HOST=${{PRIVATE_IP}}|" "$ENV_FILE"
else
  echo "WORKER_SSH_HOST=${{PRIVATE_IP}}" >> "$ENV_FILE"
fi
{capacity_block}
# Ensure MASTER_REDIS_URL points at the master ({redis_host}),
# preserving any redis://[:password@] userinfo already present.
if grep -q '^MASTER_REDIS_URL=' "$ENV_FILE"; then
  sed -i -E "s|^(MASTER_REDIS_URL=redis://([^@/]*@)?)[^:/@]+|\\1{redis_host}|" "$ENV_FILE"
else
  echo "MASTER_REDIS_URL=redis://{redis_host}:6379/0" >> "$ENV_FILE"
fi

systemctl start ops-worker-agent
{lifetime_block}"""


def _encode_user_data(script: str) -> str:
    """Base64-encode user-data (aws CLI v2 expects blobs pre-encoded)."""
    return base64.b64encode(script.encode()).decode()


def _root_block_device() -> str:
    """Root volume override for RunInstances, as a CLI JSON blob.

    Exists solely to raise gp3 throughput above the AMI's baked 125 MiB/s —
    see WORKER_ROOT_THROUGHPUT_MBPS. DeleteOnTermination stays true so a
    terminated spot worker never leaves a 300 GiB volume behind to pay for.
    """
    return json.dumps([{
        "DeviceName": WORKER_ROOT_DEVICE,
        "Ebs": {
            "VolumeType": "gp3",
            "VolumeSize": WORKER_ROOT_SIZE_GB,
            "Iops": WORKER_ROOT_IOPS,
            "Throughput": WORKER_ROOT_THROUGHPUT_MBPS,
            "DeleteOnTermination": True,
        },
    }])


def _tags_of(instance: dict) -> dict:
    """Flatten an EC2 instance's Tags list into a {Key: Value} dict."""
    return {t.get("Key"): t.get("Value") for t in instance.get("Tags", []) or []}


def _parse_instances(reservations: list) -> list[dict]:
    """Flatten describe-instances Reservations into fleet records."""
    out = []
    for res in reservations or []:
        for inst in res.get("Instances", []) or []:
            tags = _tags_of(inst)
            out.append({
                "instance_id": inst.get("InstanceId", ""),
                "name": tags.get("Name", ""),
                "type": inst.get("InstanceType", ""),
                "state": (inst.get("State") or {}).get("Name", ""),
                "private_ip": inst.get("PrivateIpAddress", ""),
                # EC2 only sets InstanceLifecycle for spot/scheduled.
                "lifecycle": inst.get("InstanceLifecycle") or "on-demand",
                "launch_time": inst.get("LaunchTime", ""),
            })
    return out


def _is_spot_worker(instance: dict) -> bool:
    """True if a raw EC2 instance dict is one of OUR disposable spot workers.

    Terminate is allowed only for instances tagged orbital-role=worker or
    named orbital-worker-spot — never for the master or pet workers.
    """
    tags = _tags_of(instance)
    return (
        tags.get("orbital-role") == WORKER_ROLE_TAG
        or tags.get("Name") == SPOT_WORKER_NAME
    )


def _verify_terminatable(descriptions: list) -> tuple[list[str], list[str]]:
    """Split raw EC2 instance dicts into (ok_ids, refused_ids).

    An id is terminatable only when the instance carries tag
    orbital-role=worker or Name=orbital-worker-spot.
    """
    ok, refused = [], []
    for inst in descriptions or []:
        iid = inst.get("InstanceId", "")
        (ok if _is_spot_worker(inst) else refused).append(iid)
    return ok, refused


def _start_stop_allowed(instance: dict) -> bool:
    """True if start/stop is permitted: any autonomous-enablements-worker*
    instance (e.g. the pre-provisioned stopped worker-3) or one of our
    tagged spot workers."""
    name = _tags_of(instance).get("Name", "") or ""
    return name.startswith(WORKER_NAME_PREFIX) or _is_spot_worker(instance)


# ── AWS CLI plumbing ─────────────────────────────────────────────────────────

async def _aws(*args: str, region: str | None = None):
    """Run the aws CLI and return parsed JSON stdout.

    Raises :class:`FleetError` with a classified message on non-zero exit
    (expired federated creds get the stable CREDS_ERROR string).
    """
    proc = await asyncio.create_subprocess_exec(
        AWS_CLI, *args, "--region", region or REGION, "--output", "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise FleetError(_classify_aws_error(err.decode(errors="replace")))
    text = out.decode(errors="replace").strip()
    return json.loads(text) if text else None


async def check_credentials(region: str = REGION) -> dict:
    """Report whether AWS access works right now, and what it allows.

    Deliberately never raises: this is the endpoint behind the dashboard's
    "check credentials" button, whose entire job is to say *plainly* that there
    are no usable credentials so they can be pasted over SSH. An exception here
    would be reported as a server error and hide the answer.

    Returns identity, expiry hint, and vCPU quota headroom — the three things
    that silently block a scale-up. Quota is per-region and On-Demand and Spot
    are separate quotas, so a fleet that fits one can still be refused.
    """
    result: dict = {
        "ok": False, "region": region, "identity": None, "account": None,
        "error": None, "quota": {}, "checked_at": None,
    }
    from datetime import datetime, timezone
    result["checked_at"] = datetime.now(timezone.utc).isoformat()

    try:
        ident = await _aws("sts", "get-caller-identity", region=region)
    except FleetError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:                      # noqa: BLE001 - never raise
        result["error"] = f"credential check failed: {exc}"
        return result

    result["ok"] = True
    result["account"] = (ident or {}).get("Account")
    arn = (ident or {}).get("Arn", "")
    result["identity"] = arn.rsplit("/", 1)[-1] if arn else None
    result["arn"] = arn

    # Quota headroom is advisory — a missing quota permission must not turn a
    # working credential into a reported failure.
    for label, code in (("on_demand", QUOTA_ONDEMAND_STANDARD),
                        ("spot", QUOTA_SPOT_STANDARD)):
        try:
            q = await _aws("service-quotas", "get-service-quota",
                           "--service-code", "ec2", "--quota-code", code,
                           region=region)
            value = ((q or {}).get("Quota") or {}).get("Value")
            result["quota"][label] = {
                "vcpus": int(value) if value is not None else None,
                "adjustable": ((q or {}).get("Quota") or {}).get("Adjustable"),
            }
        except Exception as exc:                  # noqa: BLE001
            result["quota"][label] = {"vcpus": None, "error": str(exc)[:160]}

    return result


async def region_ready(region: str) -> dict:
    """Whether we can actually launch in ``region`` today.

    An AMI id is region-scoped. Falling back to the home-region AMI in a foreign
    region launches nothing while reporting success, so a missing entry is a
    hard "not ready" with the concrete missing piece named.
    """
    missing = []
    if region not in WORKER_AMI_BY_REGION:
        missing.append("golden AMI not copied to this region")
    if region not in TEMPLATE_INSTANCE_BY_REGION:
        missing.append("no template instance for subnet/security-group lookup")
    if region not in MASTER_REDIS_BY_REGION:
        missing.append("no Redis endpoint reachable from this region (VPC peering)")
    return {
        "region": region,
        "ready": not missing,
        "missing": missing,
        "ami": WORKER_AMI_BY_REGION.get(region),
    }


async def _describe_by_ids(instance_ids: list[str]) -> list[dict]:
    """describe-instances for explicit ids → flat list of raw instance dicts."""
    data = await _aws(
        "ec2", "describe-instances", "--instance-ids", *instance_ids,
    )
    return [
        inst
        for res in (data or {}).get("Reservations", [])
        for inst in res.get("Instances", [])
    ]


# ── Public API (all async) ───────────────────────────────────────────────────

async def list_fleet() -> list[dict]:
    """List all fleet EC2 instances (tag project=autonomous-enablements OR
    Name prefix autonomous-enablements), merged and de-duplicated.

    Returns [{instance_id, name, type, state, private_ip, lifecycle,
    launch_time}].
    """
    by_tag, by_name = await asyncio.gather(
        _aws("ec2", "describe-instances", "--filters",
             f"Name=tag:project,Values={PROJECT_TAG}"),
        _aws("ec2", "describe-instances", "--filters",
             f"Name=tag:Name,Values={PROJECT_TAG}*"),
    )
    merged: dict[str, dict] = {}
    for data in (by_tag, by_name):
        for rec in _parse_instances((data or {}).get("Reservations", [])):
            if rec["instance_id"]:
                merged[rec["instance_id"]] = rec
    return sorted(merged.values(), key=lambda r: (r["name"], r["instance_id"]))


async def scale_up(count: int, instance_type: str = DEFAULT_INSTANCE_TYPE,
                   purchasing: str = "spot", lifetime_minutes: int = 0) -> list[dict]:
    """Launch ``count`` workers from the golden AMI (hard cap 4).

    Subnet, security groups and key-name are resolved at call time from the
    live worker-1 instance so launches always match production networking.
    Raises ValueError on a bad/over-cap count, FleetError on AWS failures.

    ``purchasing`` is ``"spot"`` (default, disposable capacity) or
    ``"on-demand"``. Spot instances can be reclaimed with a 2-minute warning,
    which costs a learner their session because a Sysbox session cannot be
    migrated — so run *events* on-demand and everything else on spot.

    ``lifetime_minutes``, when > 0, arms a self-destruct inside the instance:
    ``shutdown -h`` at that offset, combined with a terminate-on-shutdown
    behaviour. This deliberately does NOT depend on Orbital, on this process,
    on any AWS scheduler, or on a credential that outlives the launch — the
    box kills itself even if everything else here is dead or expired. It is
    the only cost guarantee that survives total failure of the control plane.
    """
    count = _validate_scale_count(count)

    template = await _describe_by_ids([TEMPLATE_INSTANCE_ID])
    if not template:
        raise FleetError(
            f"template instance {TEMPLATE_INSTANCE_ID} (worker-1) not found — "
            "cannot resolve subnet/security-groups/key-name"
        )
    tmpl = template[0]
    subnet_id = tmpl.get("SubnetId", "")
    key_name = tmpl.get("KeyName", "")
    sg_ids = [g["GroupId"] for g in tmpl.get("SecurityGroups", []) or []]
    if not (subnet_id and sg_ids):
        raise FleetError(
            f"template instance {TEMPLATE_INSTANCE_ID} has no subnet/"
            "security groups (is it terminated?)"
        )

    purchasing = (purchasing or "spot").strip().lower()
    if purchasing not in ("spot", "on-demand"):
        raise ValueError(f"purchasing must be 'spot' or 'on-demand', got {purchasing!r}")
    market_options = json.dumps({
        "MarketType": "spot",
        "SpotOptions": {"SpotInstanceType": "one-time"},
    })
    tag_spec = (
        "ResourceType=instance,Tags=["
        f"{{Key=Name,Value={SPOT_WORKER_NAME}}},"
        f"{{Key=project,Value={PROJECT_TAG}}},"
        # MANDATORY. The OrbitalFleetAutoscaler IAM role may only launch, stop,
        # start or terminate instances carrying this exact tag; without it every
        # call fails UnauthorizedOperation. It is also the blast-radius guarantee
        # that the role can never touch an instance it did not create.
        f"{{Key={FLEET_TAG_KEY},Value={FLEET_TAG_VALUE}}},"
        f"{{Key=orbital-role,Value={WORKER_ROLE_TAG}}}]"
    )

    args = [
        "ec2", "run-instances",
        "--image-id", WORKER_AMI,
        "--count", str(count),
        "--instance-type", instance_type,
        "--subnet-id", subnet_id,
        "--security-group-ids", *sg_ids,
        "--tag-specifications", tag_spec,
        "--block-device-mappings", _root_block_device(),
        "--user-data", _encode_user_data(_build_user_data(lifetime_minutes=lifetime_minutes)),
    ]
    if purchasing == "spot":
        args += ["--instance-market-options", market_options]
    if lifetime_minutes > 0:
        # Belt to the user-data's braces: if the box ever halts for any reason,
        # it terminates rather than lingering as a stopped instance we still
        # pay EBS on and still have to remember to clean up.
        args += ["--instance-initiated-shutdown-behavior", "terminate"]
    if key_name:
        args += ["--key-name", key_name]

    data = await _aws(*args)
    return [
        {
            "instance_id": inst.get("InstanceId", ""),
            "type": inst.get("InstanceType", ""),
            "state": (inst.get("State") or {}).get("Name", ""),
            "lifecycle": inst.get("InstanceLifecycle") or "on-demand",
        }
        for inst in (data or {}).get("Instances", [])
    ]


async def scale_down(instance_ids: list[str]) -> list[dict]:
    """Terminate spot workers — refuses unless EVERY id is tagged
    orbital-role=worker or Name=orbital-worker-spot.

    Raises FleetError listing the refused ids when any id fails the check.
    """
    if not instance_ids:
        raise ValueError("instance_ids is required")

    descriptions = await _describe_by_ids(instance_ids)
    ok_ids, refused = _verify_terminatable(descriptions)
    # Ids that describe-instances didn't return at all are refused too.
    described = {inst.get("InstanceId") for inst in descriptions}
    refused += [iid for iid in instance_ids if iid not in described]
    if refused:
        raise FleetError(
            "refusing to terminate non-spot-worker instance(s): "
            + ", ".join(sorted(refused))
            + " — only instances tagged orbital-role=worker or "
              "Name=orbital-worker-spot may be terminated"
        )

    data = await _aws("ec2", "terminate-instances", "--instance-ids", *ok_ids)
    return [
        {
            "instance_id": t.get("InstanceId", ""),
            "previous_state": (t.get("PreviousState") or {}).get("Name", ""),
            "current_state": (t.get("CurrentState") or {}).get("Name", ""),
        }
        for t in (data or {}).get("TerminatingInstances", [])
    ]


async def _start_stop(instance_id: str, action: str) -> dict:
    """Shared guard + CLI call for start_worker / stop_worker."""
    descriptions = await _describe_by_ids([instance_id])
    if not descriptions:
        raise FleetError(f"instance {instance_id} not found")
    if not _start_stop_allowed(descriptions[0]):
        raise FleetError(
            f"refusing to {action} {instance_id} — only "
            f"{WORKER_NAME_PREFIX}* instances or tagged spot workers "
            "may be started/stopped"
        )
    verb = "start-instances" if action == "start" else "stop-instances"
    key = "StartingInstances" if action == "start" else "StoppingInstances"
    data = await _aws("ec2", verb, "--instance-ids", instance_id)
    states = (data or {}).get(key, [])
    rec = states[0] if states else {}
    return {
        "instance_id": instance_id,
        "previous_state": (rec.get("PreviousState") or {}).get("Name", ""),
        "current_state": (rec.get("CurrentState") or {}).get("Name", ""),
    }


async def start_worker(instance_id: str) -> dict:
    """Start a stopped pet worker (e.g. worker-3, i-03689a1374d39cb6a)."""
    return await _start_stop(instance_id, "start")


async def stop_worker(instance_id: str) -> dict:
    """Stop a running worker (allowed for autonomous-enablements-worker*)."""
    return await _start_stop(instance_id, "stop")
