"""Seed a launched worker with the CURRENT master Redis credential.

The golden worker AMI bakes a whole ``/home/ops/.env``, including
``MASTER_REDIS_PASSWORD`` as it stood on the day the image was made (v2:
2026-07-15). cloud-init's user-data rewrites the worker's identity, pool,
capacity and Redis *host*, but deliberately leaves the credential alone — so
the first rotation of the master Redis password silently turned every future
autoscaled worker into a crash-loop:

    redis.exceptions.AuthenticationError: invalid username-password pair ...

on the agent's opening ``ping()``. The worker never writes a ``worker:*``
registration, ``_pool_workers_ready`` therefore counts zero forever, and the
workshop sits at ``warming`` until the degraded timeout with nothing in the
master's logs to say why — the failure is entirely on the worker, which is the
one box an operator does not think to look at. Observed live 2026-08-27, on the
first autoscaled workshop after the 2026-08-26 rotation.

WHY OVER SSH AND NOT IN USER-DATA
The obvious fix — write the password into the cloud-init script — would put a
live credential into EC2 user-data, which is readable from IMDS by anything on
the instance. Workers run ``HttpPutResponseHopLimit=2``, so that includes every
learner's Sysbox slot. This module uses the master→worker ssh channel the PTY
bridge already depends on instead, so the secret never leaves an authenticated
connection.

The password is fed to the remote shell on **stdin**, never in argv: argv is
world-readable through ``/proc`` on both ends, and a learner shares the worker
with this command. For the same reason the remote script edits the env file
with shell builtins (``read``, ``printf``) rather than handing the value to
``sed``.

Best-effort and idempotent by design. It is safe to call on every control-loop
tick for any worker that has not registered yet: a worker whose credential is
already correct is left completely alone (no rewrite, no agent restart), so the
steady-state cost is one ssh round-trip per unregistered instance and nothing
else. That also means it repairs a worker launched *before* a rotation, not
only one launched after.
"""

import asyncio
import logging
import os
import shlex
from urllib.parse import urlsplit, unquote

from shared.log_safety import scrub_for_log

from shared import environment

log = logging.getLogger(__name__)

# Where the agent's env lives on a worker, and the unit that reads it.
WORKER_ENV_FILE = "/home/ops/.env"
WORKER_AGENT_UNIT = "ops-worker-agent"

# The control loop runs as `ops` on the master and connects as `ubuntu` on the
# worker — VERIFIED, not assumed: `ops` -> `ubuntu@worker` succeeds with a
# working `sudo -n`, while `ops` -> `ops@worker` is "Permission denied
# (publickey)". Do not "correct" this to `ops`; the remote half needs sudo and
# `ops` has none (it is also what made a password rotation fail once already).
# BatchMode so a missing key fails fast instead of hanging on a password prompt
# inside the control loop.
SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=8",
)
SSH_USER = "ubuntu"
SEED_TIMEOUT_SECONDS = 45


# ── Pure helpers (unit-tested, no subprocess / no network) ───────────────────

def password_from_url(url: str) -> str:
    """Extract the password from a ``redis://:pw@host:port/db`` URL.

    Returns "" when there is no userinfo — an unauthenticated Redis is a valid
    configuration and must not be reported as a parse failure.
    """
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return ""
    return unquote(parsed.password or "")


def master_redis_password() -> str:
    """The password a worker must present to the master's Redis, or "".

    ``REDIS_URL`` is the authority because it is what the master itself
    connects with; ``REDIS_PASSWORD`` is the fallback for a deployment that
    keeps them apart. Returning "" means "we do not know the credential" and
    every caller treats that as *do nothing* — seeding a worker with an empty
    password would replace a stale credential with a guaranteed-wrong one.
    """
    from_url = password_from_url(os.environ.get("REDIS_URL", ""))
    return from_url or os.environ.get("REDIS_PASSWORD", "")


def worker_id_for_instance(instance_id: str) -> str:
    """The WORKER_ID cloud-init derives for ``instance_id``, or "".

    Mirrors ``fleet._build_user_data``: the last 8 characters of the instance
    id, prefixed. Kept as a function rather than repeated inline so the two
    cannot drift — if the user-data naming changes, this is the one other place
    that has to change with it, and the test pins them together.
    """
    instance_id = (instance_id or "").strip()
    if not instance_id.startswith("i-") or len(instance_id) < 10:
        return ""
    return f"worker-x86_64-spot-{instance_id[-8:]}"


def build_seed_script(env_file: str = WORKER_ENV_FILE,
                      unit: str = WORKER_AGENT_UNIT) -> str:
    """The remote shell script. Reads the password from stdin, never argv.

    Contract, in order:
      1. Read one line from stdin. Refuse an empty password outright — see
         ``master_redis_password``.
      2. Compare against what the file already holds. Identical → exit 0 having
         changed nothing and, critically, WITHOUT restarting the agent: this
         runs every tick against workers that are merely still booting, and
         bouncing the unit under them would restart the Sysbox pool warm-up
         from zero each time and the worker would never reach ready.
      3. Rewrite via a temp file, then verify the rewrite kept the rest of the
         file before installing it. A truncated /home/ops/.env would take the
         worker from "wrong password" to "no configuration at all", which is
         strictly worse and much harder to see.
      4. Restart the agent so it picks the value up — the unit reads the env
         file at start, so a rewrite alone deploys nothing.

    Every exit path prints one stable token the caller classifies on, because
    ssh's exit code alone cannot distinguish "already correct" from "repaired".
    """
    return f"""set -eu
umask 077
read -r PW || true
if [ -z "${{PW:-}}" ]; then echo "SEED_NO_PASSWORD"; exit 0; fi

ENV_FILE={env_file}
# `sudo -n`, not a bare test: /home/ops is drwxr-x--- and this script runs as
# `ubuntu`, so an unprivileged existence check reports "missing" on every worker
# even though the file is there. Every other access here is already privileged;
# this one was not, and it made the script a guaranteed no-op.
if ! sudo -n test -f "$ENV_FILE"; then echo "SEED_NO_ENV_FILE"; exit 0; fi

CURRENT=$(sudo -n grep -m1 '^MASTER_REDIS_PASSWORD=' "$ENV_FILE" 2>/dev/null \\
  | cut -d= -f2- || true)
if [ "$CURRENT" = "$PW" ]; then
    # The file is right -- but the agent may still be running with the OLD value
    # in memory (a rewrite whose restart failed, or a restart that crash-looped).
    # Only act when the unit is actually broken: a merely-booting worker is
    # `active`/`activating`, and bouncing it would restart the Sysbox warm-up
    # from zero and it would never reach ready.
    if sudo -n systemctl is-failed --quiet {unit}; then
        if sudo -n systemctl restart {unit}; then
            echo "SEED_RESTARTED_STALE_AGENT"; exit 0
        fi
    fi
    echo "SEED_ALREADY_CURRENT"; exit 0
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
sudo -n grep -v '^MASTER_REDIS_PASSWORD=' "$ENV_FILE" > "$TMP" 2>/dev/null || true
# printf is a shell builtin, so the password never appears in any argv.
printf 'MASTER_REDIS_PASSWORD=%s\\n' "$PW" >> "$TMP"

# Refuse to install a file that lost the worker's identity: that is the
# signature of a truncated read, and shipping it would brick the worker.
if ! grep -q '^WORKER_ID=' "$TMP"; then echo "SEED_REFUSED_TRUNCATED"; exit 1; fi

sudo -n install -m 600 -o ops -g ops "$TMP" "$ENV_FILE"
# `set -e` would abort here on a failed restart -- BEFORE the token is printed.
# The caller would see "unknown" while the file was in fact rewritten, and the
# next tick would find the credential already correct and never restart the
# agent, leaving the worker permanently unrepairable. Report the two outcomes
# apart instead of dying between them.
if sudo -n systemctl restart {unit}; then
    echo "SEED_UPDATED"
else
    echo "SEED_UPDATED_NO_RESTART"
fi
"""


def classify_seed_output(stdout: str) -> str:
    """Map the remote script's token to a status, or "unknown".

    Kept separate from the subprocess call so the whole decision table is
    testable without ssh.
    """
    text = stdout or ""
    for token, status in (
        ("SEED_UPDATED_NO_RESTART", "updated-no-restart"),
        ("SEED_RESTARTED_STALE_AGENT", "restarted-stale-agent"),
        ("SEED_UPDATED", "updated"),
        ("SEED_ALREADY_CURRENT", "already-current"),
        ("SEED_REFUSED_TRUNCATED", "refused"),
        ("SEED_NO_ENV_FILE", "no-env-file"),
        ("SEED_NO_PASSWORD", "no-password"),
    ):
        if token in text:
            return status
    return "unknown"


def unregistered_instances(instance_ips: dict[str, str],
                           registered_worker_ids: set[str]) -> list[tuple[str, str]]:
    """(instance_id, ip) pairs whose worker has not registered in Redis.

    An instance with no private IP yet is skipped rather than guessed at: it is
    still coming up, and there is nothing to ssh to.
    """
    out = []
    for instance_id, ip in sorted((instance_ips or {}).items()):
        if not ip:
            continue
        wid = worker_id_for_instance(instance_id)
        if wid and wid not in registered_worker_ids:
            out.append((instance_id, ip))
    return out


def _redact(text: str, secret: str) -> str:
    """Remove ``secret`` from text that is about to be logged.

    The remote shell echoes a failed command back on stderr, so a bug that puts
    the password where a command belongs puts the password in the log -- which
    is exactly what happened on 2026-08-27: the credential reached
    /var/log/syslog and, through the host OneAgent, production Grail. Logging
    raw remote stderr is therefore never safe, no matter how confident the
    caller is about what that stderr can contain.
    """
    out = (text or "").strip()
    if secret:
        out = out.replace(secret, "<redacted>")
    return scrub_for_log(out)


# ── Side-effecting ──────────────────────────────────────────────────────────

async def seed_worker(host: str, password: str = "",
                      timeout: float = SEED_TIMEOUT_SECONDS) -> str:
    """Push the current Redis credential to ``host``. Returns a status string.

    Never raises: this runs inside the control loop, where one unreachable
    worker must not stop the tick from serving the rest. A worker that is still
    booting simply refuses the connection and comes back "unreachable"; the
    next tick tries again.
    """
    password = password or master_redis_password()
    if not password:
        return "no-password"
    if not host:
        return "no-host"

    # `bash -s` reads its SCRIPT from stdin -- and stdin is where the password
    # goes. Using it meant the remote bash executed the password as a command
    # ("<secret>: command not found", rc=127) and the script was never sent at
    # all. The script therefore travels in argv (it is not secret) and stdin is
    # reserved for the password (which is). shlex.quote because ssh joins its
    # remaining arguments with spaces and the REMOTE shell re-parses the result.
    remote = f"bash -c {shlex.quote(build_seed_script())}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *SSH_OPTS, f"{SSH_USER}@{host}", remote,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.warning("worker seed: cannot start ssh to %s: %s", host, exc)
        return "unreachable"

    payload = f"{password}\n".encode()
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=payload), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        # Drain so the child is reaped rather than left as a zombie holding a pipe.
        try:
            await proc.communicate()
        except Exception:
            pass
        log.warning("worker seed: ssh to %s timed out after %ss", host, timeout)
        return "timeout"

    status = classify_seed_output(out.decode(errors="replace"))
    if status == "unknown":
        # stderr, never stdout: the script prints only its own tokens, so
        # anything else came from ssh or sudo and is diagnostic, not secret.
        detail = _redact(err.decode(errors="replace"), password)[:200]
        if proc.returncode != 0:
            log.warning("worker seed: %s failed (rc=%s): %s",
                        host, proc.returncode, detail)
            return "unreachable"
        log.warning("worker seed: %s gave no recognised status: %s", host, detail)
    elif status == "updated":
        log.info("worker seed: %s had a stale master Redis credential — "
                 "rewritten and %s restarted", host, WORKER_AGENT_UNIT)
    elif status == "updated-no-restart":
        log.error("worker seed: %s credential rewritten but %s FAILED to "
                  "restart - it is still running the old value", host, WORKER_AGENT_UNIT)
    elif status == "restarted-stale-agent":
        log.warning("worker seed: %s already had the right credential but its "
                    "agent had failed - restarted", host)
    elif status == "refused":
        log.error("worker seed: %s refused the rewrite (env file looked "
                  "truncated) — left untouched", host)
    return status


async def reconcile(redis, instance_ids: list[str]) -> dict[str, str]:
    """Seed every instance in ``instance_ids`` that has not registered yet.

    Returns {instance_id: status}. Empty when there is nothing to do, which is
    the normal steady state — an instance whose worker is already in Redis is
    never contacted.
    """
    from dashboard import fleet

    if not instance_ids:
        return {}
    password = master_redis_password()
    if not password:
        # Said once, loudly: with no credential to push, a stale worker can
        # never be repaired and the operator needs to know that is why.
        log.warning("worker seed: master Redis password unknown (REDIS_URL has "
                    "no userinfo and REDIS_PASSWORD is unset) — cannot repair "
                    "stale workers")
        return {}

    registered: set[str] = set()
    try:
        async for key in redis.scan_iter(match="worker:*", count=200):
            if key.count(":") == 1:
                registered.add(key.split(":", 1)[1])
    except Exception as exc:
        # A failed scan makes every worker look unregistered, which would ssh
        # to the whole fleet and restart healthy agents. Do nothing instead.
        log.warning("worker seed: cannot list registered workers: %s", exc)
        return {}

    try:
        described = await fleet._describe_by_ids(list(instance_ids))
    except Exception as exc:
        log.warning("worker seed: cannot describe instances: %s", exc)
        return {}

    # Environment guard. Seeding is a MUTATING remote action -- it rewrites
    # /home/ops/.env and restarts the agent -- so it belongs with the other
    # cross-environment guards (`_is_spot_worker`, `_start_stop_allowed`), not
    # only behind the discovery filter. The instance ids arrive from a fleet
    # record in this environment's own Redis, so this should never fire; that
    # is the point. A guard that only holds while every caller is correct is
    # not a guard. Untagged reads as prod, so production still repairs the
    # long-lived machines that predate the `env` tag.
    env_name = environment.current().name
    ips = {}
    for inst in described:
        if (inst.get("State") or {}).get("Name") != "running":
            continue
        if not environment.owns(fleet._tags_of(inst), env_name):
            log.warning("worker seed: refusing %s -- it belongs to a different "
                        "environment", inst.get("InstanceId", "?"))
            continue
        ips[inst.get("InstanceId", "")] = inst.get("PrivateIpAddress", "")

    results: dict[str, str] = {}
    for instance_id, ip in unregistered_instances(ips, registered):
        results[instance_id] = await seed_worker(ip, password)
    return results
