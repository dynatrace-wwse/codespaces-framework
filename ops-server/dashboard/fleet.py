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
REGION = "eu-west-2"

# Golden worker AMI (pre-baked Sysbox + ops-worker-agent).
WORKER_AMI = "ami-0ed76cf85fa7d2967"
# worker-1 — template instance whose subnet / security groups / key-name are
# resolved dynamically at scale-up time so networking never drifts from prod.
TEMPLATE_INSTANCE_ID = "i-02b773319c758fe40"
# Master's private IP — spot workers must point MASTER_REDIS_URL here.
MASTER_REDIS_HOST = "172.31.36.172"

PROJECT_TAG = "autonomous-enablements"
SPOT_WORKER_NAME = "orbital-worker-spot"
WORKER_ROLE_TAG = "worker"           # value of the orbital-role tag
WORKER_NAME_PREFIX = "autonomous-enablements-worker"

# Hard safety limit: never launch more than this many instances per call.
MAX_SCALE_UP = 4

DEFAULT_INSTANCE_TYPE = "c5.2xlarge"

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


def _build_user_data() -> str:
    """Cloud-init user-data shell script for a fresh spot worker.

    - Derives a unique WORKER_ID from the instance id (IMDSv2, token-based).
    - Ensures WORKER_ID= in /home/ops/.env (sed-replace existing line, else
      append).
    - Ensures MASTER_REDIS_URL points at the master's private IP
      (sed-rewrites the host part of an existing redis:// URL — password
      userinfo preserved — else appends a default URL).
    - Restarts ops-worker-agent so it registers with the new identity.
    """
    return f"""#!/bin/bash
set -uo pipefail
ENV_FILE=/home/ops/.env

# IMDSv2: fetch a session token, then the instance id (last 8 chars).
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \\
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \\
  "http://169.254.169.254/latest/meta-data/instance-id" | tail -c 8)
WORKER_ID="worker-x86_64-spot-${{INSTANCE_ID}}"

touch "$ENV_FILE"

# Set the unique WORKER_ID (replace existing line or append).
if grep -q '^WORKER_ID=' "$ENV_FILE"; then
  sed -i "s|^WORKER_ID=.*|WORKER_ID=${{WORKER_ID}}|" "$ENV_FILE"
else
  echo "WORKER_ID=${{WORKER_ID}}" >> "$ENV_FILE"
fi

# Ensure MASTER_REDIS_URL points at the master ({MASTER_REDIS_HOST}),
# preserving any redis://[:password@] userinfo already present.
if grep -q '^MASTER_REDIS_URL=' "$ENV_FILE"; then
  sed -i -E "s|^(MASTER_REDIS_URL=redis://([^@/]*@)?)[^:/@]+|\\1{MASTER_REDIS_HOST}|" "$ENV_FILE"
else
  echo "MASTER_REDIS_URL=redis://{MASTER_REDIS_HOST}:6379/0" >> "$ENV_FILE"
fi

systemctl restart ops-worker-agent
"""


def _encode_user_data(script: str) -> str:
    """Base64-encode user-data (aws CLI v2 expects blobs pre-encoded)."""
    return base64.b64encode(script.encode()).decode()


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

async def _aws(*args: str):
    """Run the aws CLI and return parsed JSON stdout.

    Raises :class:`FleetError` with a classified message on non-zero exit
    (expired federated creds get the stable CREDS_ERROR string).
    """
    proc = await asyncio.create_subprocess_exec(
        AWS_CLI, *args, "--region", REGION, "--output", "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise FleetError(_classify_aws_error(err.decode(errors="replace")))
    text = out.decode(errors="replace").strip()
    return json.loads(text) if text else None


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


async def scale_up(count: int, instance_type: str = DEFAULT_INSTANCE_TYPE) -> list[dict]:
    """Launch ``count`` spot workers from the golden AMI (hard cap 4).

    Subnet, security groups and key-name are resolved at call time from the
    live worker-1 instance so launches always match production networking.
    Raises ValueError on a bad/over-cap count, FleetError on AWS failures.
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

    market_options = json.dumps({
        "MarketType": "spot",
        "SpotOptions": {"SpotInstanceType": "one-time"},
    })
    tag_spec = (
        "ResourceType=instance,Tags=["
        f"{{Key=Name,Value={SPOT_WORKER_NAME}}},"
        f"{{Key=project,Value={PROJECT_TAG}}},"
        f"{{Key=orbital-role,Value={WORKER_ROLE_TAG}}}]"
    )

    args = [
        "ec2", "run-instances",
        "--image-id", WORKER_AMI,
        "--count", str(count),
        "--instance-type", instance_type,
        "--subnet-id", subnet_id,
        "--security-group-ids", *sg_ids,
        "--instance-market-options", market_options,
        "--tag-specifications", tag_spec,
        "--user-data", _encode_user_data(_build_user_data()),
    ]
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
