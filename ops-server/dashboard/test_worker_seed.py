"""Tests for the worker credential seeder (dashboard/worker_seed.py).

No ssh, no AWS, no Redis: the module keeps every decision in pure helpers so
the dangerous half (a shell script that rewrites /home/ops/.env on a live
worker and restarts its agent) can be pinned without touching a machine.

Runnable two ways:
  - pytest:     python3 -m pytest dashboard/test_worker_seed.py
  - standalone: python3 -m dashboard.test_worker_seed
"""

import asyncio

from dashboard import worker_seed

# Sentinel values, not credentials. Each is a marker the assertions match on to
# prove WHICH SOURCE a value was read from. They are named constants rather than
# literals written next to a *_PASSWORD key, because generic secret scanners
# flag that shape on sight -- and a red security check on a test fixture trains
# people to merge past red security checks.
SENTINEL_FROM_URL = "sentinel-value-url"
SENTINEL_FROM_ENV = "sentinel-value-env"
SENTINEL_ANY = "sentinel-value-any"



# ── Credential discovery ─────────────────────────────────────────────────────

def test_password_parsed_from_redis_url():
    assert worker_seed.password_from_url(
        f"redis://:{SENTINEL_ANY}@redis.invalid:6379/0") == SENTINEL_ANY


def test_percent_encoded_password_is_decoded():
    # A rotation can produce a password with URL-reserved characters; handing
    # the still-encoded form to a worker would be a silent wrong-password.
    encoded, decoded = "a%40b%3Ac", "a@b:c"   # '@' and ':' percent-encoded
    assert worker_seed.password_from_url(
        f"redis://:{encoded}@host:6379/0") == decoded


def test_url_without_userinfo_yields_empty_not_error():
    assert worker_seed.password_from_url("redis://redis.invalid:6379/0") == ""
    assert worker_seed.password_from_url("") == ""
    assert worker_seed.password_from_url("not a url at all") == ""


def test_master_password_prefers_url_over_bare_env(monkeypatch=None):
    import os
    old = dict(os.environ)
    try:
        os.environ["REDIS_URL"] = f"redis://:{SENTINEL_FROM_URL}@localhost:6379/0"
        os.environ["REDIS_PASSWORD"] = SENTINEL_FROM_ENV
        assert worker_seed.master_redis_password() == SENTINEL_FROM_URL
        os.environ["REDIS_URL"] = "redis://localhost:6379/0"
        assert worker_seed.master_redis_password() == SENTINEL_FROM_ENV
    finally:
        os.environ.clear()
        os.environ.update(old)


# ── Worker id derivation (must mirror fleet._build_user_data) ────────────────

def test_worker_id_is_the_last_eight_chars_of_the_instance_id():
    assert (worker_seed.worker_id_for_instance("i-0338b3e57be96a717")
            == "worker-x86_64-spot-be96a717")


def test_worker_id_refuses_a_non_instance_id():
    for bad in ("", "abc", "i-short", None):
        assert worker_seed.worker_id_for_instance(bad) == ""


def test_worker_id_matches_the_user_data_that_creates_it():
    # The two live in different modules and would drift silently: cloud-init
    # names the worker, this module has to find it again in Redis by that name.
    from dashboard import fleet
    script = fleet._build_user_data()
    assert 'WORKER_ID="worker-x86_64-spot-${INSTANCE_ID}"' in script
    assert worker_seed.worker_id_for_instance("i-00000000deadbeef").startswith(
        "worker-x86_64-spot-")


# ── The remote script ────────────────────────────────────────────────────────

def test_script_reads_the_password_from_stdin_not_argv():
    script = worker_seed.build_seed_script()
    assert "read -r PW" in script
    # printf is a builtin; sed/echo with the value would expose it in /proc to
    # every learner container sharing the worker.
    assert "printf 'MASTER_REDIS_PASSWORD=%s\\n' \"$PW\"" in script
    assert "sed -i" not in script


def test_a_healthy_worker_with_the_right_credential_is_never_bounced():
    """A merely-warming worker must not be restarted.

    Bouncing it restarts the Sysbox pool warm-up from zero and it never reaches
    ready. Asserted as a property of the branch rather than as a text offset:
    the already-current path DOES contain a restart now (for an agent that has
    actually failed), so comparing string positions no longer says anything
    about behaviour -- what matters is that the restart is GUARDED.
    """
    script = worker_seed.build_seed_script()
    head, _, tail = script.partition("SEED_ALREADY_CURRENT")
    branch = head[head.index('if [ "$CURRENT" = "$PW" ]'):]
    assert "systemctl is-failed" in branch, (
        "the restart on the already-current path is unguarded -- a warming "
        "worker would be bounced on every tick")
    # ...and the guard has to gate the restart, not merely appear nearby.
    assert branch.index("systemctl is-failed") < branch.index("systemctl restart")


def test_script_refuses_to_install_a_truncated_env_file():
    script = worker_seed.build_seed_script()
    assert "SEED_REFUSED_TRUNCATED" in script
    assert script.index("SEED_REFUSED_TRUNCATED") < script.index("install -m 600")


def test_script_restarts_the_agent_after_a_rewrite():
    # The unit reads the env file at start, so a rewrite alone deploys nothing.
    script = worker_seed.build_seed_script()
    unit = worker_seed.WORKER_AGENT_UNIT
    assert f"systemctl restart {unit}" in script
    # Look only at what follows the install, so the stale-agent restart earlier
    # in the script cannot satisfy this by accident.
    after_install = script[script.index("install -m 600"):]
    assert f"systemctl restart {unit}" in after_install, \
        "the rewrite path does not restart the agent; the new value is never read"


def test_script_rejects_an_empty_password():
    script = worker_seed.build_seed_script()
    assert "SEED_NO_PASSWORD" in script


# ── Output classification ────────────────────────────────────────────────────

def test_every_token_classifies():
    cases = {
        "SEED_UPDATED\n": "updated",
        "SEED_ALREADY_CURRENT\n": "already-current",
        "SEED_REFUSED_TRUNCATED\n": "refused",
        "SEED_NO_ENV_FILE\n": "no-env-file",
        "SEED_NO_PASSWORD\n": "no-password",
        "": "unknown",
        "some ssh noise": "unknown",
    }
    for out, expected in cases.items():
        assert worker_seed.classify_seed_output(out) == expected, out


# ── Which instances get contacted ────────────────────────────────────────────

def test_registered_workers_are_never_contacted():
    ips = {"i-0000000011111111": "10.0.0.1"}
    registered = {"worker-x86_64-spot-11111111"}
    assert worker_seed.unregistered_instances(ips, registered) == []


def test_unregistered_worker_is_selected():
    ips = {"i-0000000011111111": "10.0.0.1"}
    assert worker_seed.unregistered_instances(ips, set()) == [
        ("i-0000000011111111", "10.0.0.1")]


def test_instance_without_an_ip_is_skipped():
    # Still coming up — there is nothing to ssh to, and guessing an address is
    # how you seed the wrong box.
    assert worker_seed.unregistered_instances(
        {"i-0000000011111111": ""}, set()) == []


# ── Failure containment ──────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self, keys=(), fail=False):
        self._keys, self._fail = list(keys), fail

    async def scan_iter(self, match=None, count=None):
        if self._fail:
            raise RuntimeError("redis down")
        for k in self._keys:
            yield k

    # Make the async generator usable with `async for` in the module.
    def __call__(self, *a, **kw):
        return self


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_reconcile_does_nothing_without_a_known_password():
    import os
    old = dict(os.environ)
    try:
        os.environ.pop("REDIS_URL", None)
        os.environ.pop("REDIS_PASSWORD", None)
        assert _run(worker_seed.reconcile(_FakeRedis(), ["i-0000000011111111"])) == {}
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_reconcile_touches_nothing_when_the_scan_fails():
    # A failed scan makes EVERY worker look unregistered. Seeding on that
    # reading would ssh the whole fleet and restart healthy agents mid-session.
    import os
    old = dict(os.environ)
    try:
        os.environ["REDIS_URL"] = f"redis://:{SENTINEL_ANY}@localhost:6379/0"
        assert _run(worker_seed.reconcile(
            _FakeRedis(fail=True), ["i-0000000011111111"])) == {}
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_reconcile_with_no_instances_is_a_no_op():
    assert _run(worker_seed.reconcile(_FakeRedis(), [])) == {}


def test_seed_worker_without_a_host_reports_no_host():
    assert _run(worker_seed.seed_worker("", password=SENTINEL_ANY)) == "no-host"


def test_seed_worker_without_a_password_reports_no_password():
    import os
    old = dict(os.environ)
    try:
        os.environ.pop("REDIS_URL", None)
        os.environ.pop("REDIS_PASSWORD", None)
        assert _run(worker_seed.seed_worker("10.0.0.1")) == "no-password"
    finally:
        os.environ.clear()
        os.environ.update(old)



# ── Environment guard ────────────────────────────────────────────────────────
# Seeding rewrites a remote /home/ops/.env and restarts the agent, so it is a
# mutating cross-host action and must not reach another environment's machines.

def _inst(iid, env_value=None, state="running", ip="10.0.0.9"):
    tags = [{"Key": "Name", "Value": "autonomous-enablements-worker"}]
    if env_value is not None:
        tags.append({"Key": "env", "Value": env_value})
    return {"InstanceId": iid, "PrivateIpAddress": ip,
            "State": {"Name": state}, "Tags": tags}


class _FakeRedisWithKeys:
    """Distinct from the module's other fake: this one is seeded with the exact
    worker:* keys the scan should return."""
    def __init__(self, keys): self._keys = list(keys)
    async def scan_iter(self, match=None, count=None):
        for k in self._keys:
            yield k


def _reconcile_with(described, env_name, monkey_env="prod"):
    """Run reconcile against a stubbed fleet/describe, return {iid: status}."""
    import os
    from dashboard import fleet
    seen = []

    async def fake_describe(ids):
        return described
    async def fake_seed(host, password="", timeout=None):
        seen.append(host)
        return "updated"

    old_desc = fleet._describe_by_ids
    old_seed = worker_seed.seed_worker
    old_env = os.environ.get("ORBITAL_ENV")
    old_pw = os.environ.get("REDIS_PASSWORD")
    try:
        fleet._describe_by_ids = fake_describe
        worker_seed.seed_worker = fake_seed
        os.environ["ORBITAL_ENV"] = env_name
        os.environ["REDIS_PASSWORD"] = SENTINEL_ANY
        os.environ.pop("REDIS_URL", None)
        res = _run(worker_seed.reconcile(_FakeRedisWithKeys([]), [d["InstanceId"] for d in described]))
        return res, seen
    finally:
        fleet._describe_by_ids = old_desc
        worker_seed.seed_worker = old_seed
        if old_env is None: os.environ.pop("ORBITAL_ENV", None)
        else: os.environ["ORBITAL_ENV"] = old_env
        if old_pw is None: os.environ.pop("REDIS_PASSWORD", None)
        else: os.environ["REDIS_PASSWORD"] = old_pw


def test_seeds_an_instance_it_owns():
    res, seen = _reconcile_with([_inst("i-aaaaaaaaaaaaaaaa1", "prod")], "prod")
    assert seen == ["10.0.0.9"], f"own-environment instance was not seeded: {seen}"
    assert res.get("i-aaaaaaaaaaaaaaaa1") == "updated"


def test_refuses_an_instance_owned_by_another_environment():
    # The guard that matters: staging must never restart a production agent.
    res, seen = _reconcile_with([_inst("i-bbbbbbbbbbbbbbbb2", "prod")], "staging")
    assert seen == [], f"staging seeded a prod instance: {seen}"
    assert res == {}


def test_prod_refuses_a_staging_instance():
    res, seen = _reconcile_with([_inst("i-cccccccccccccccc3", "staging")], "prod")
    assert seen == [], f"prod seeded a staging instance: {seen}"


def test_untagged_instance_is_repaired_by_production():
    # The long-lived machines predate the env tag; prod must still repair them,
    # or this guard would strand exactly the workers it is meant to protect.
    res, seen = _reconcile_with([_inst("i-dddddddddddddddd4", None)], "prod")
    assert seen == ["10.0.0.9"], "prod refused an untagged (legacy) instance"


def test_untagged_instance_is_never_touched_by_staging():
    res, seen = _reconcile_with([_inst("i-eeeeeeeeeeeeeeee5", None)], "staging")
    assert seen == [], "staging claimed a legacy instance"


def test_guard_is_not_vacuous():
    # Mutation check: the ownership test must be what rejects, not the state
    # filter or an empty describe. Same instance, only the env differs.
    _, seen_ok = _reconcile_with([_inst("i-ffffffffffffffff6", "staging")], "staging")
    _, seen_no = _reconcile_with([_inst("i-ffffffffffffffff6", "prod")], "staging")
    assert seen_ok and not seen_no, (
        "guard is vacuous: identical input differing only in env tag gave "
        f"{seen_ok!r} and {seen_no!r}")


# ── The invocation itself ────────────────────────────────────────────────────
# These exist because 1113 tests passed while seed_worker could not work at all:
# everything stubbed the subprocess or tested only the pure helpers, so nothing
# checked that the script is delivered or that the password stays out of argv.

def _captured_exec():
    """Run seed_worker against a stubbed exec and return (argv, stdin_bytes)."""
    import asyncio as _a
    captured = {}

    class _Proc:
        returncode = 0
        async def communicate(self, input=None):
            captured["stdin"] = input
            return b"SEED_ALREADY_CURRENT", b""

    async def fake_exec(*argv, **kw):
        captured["argv"] = argv
        return _Proc()

    real = _a.create_subprocess_exec
    _a.create_subprocess_exec = fake_exec
    try:
        _run(worker_seed.seed_worker("10.0.0.5", password=SENTINEL_ANY))
    finally:
        _a.create_subprocess_exec = real
    return captured.get("argv", ()), captured.get("stdin", b"")


def test_the_remote_command_actually_carries_the_script():
    # `bash -s` reads the script from STDIN, which is where the password goes --
    # so the script was never delivered and the password was executed as a
    # command. Pin that the script travels in argv.
    argv, _ = _captured_exec()
    joined = " ".join(argv)
    assert "bash -c" in joined, f"remote command is not `bash -c`: {joined[-120:]!r}"
    for token in ("MASTER_REDIS_PASSWORD", "SEED_ALREADY_CURRENT", "read -r PW"):
        assert token in joined, f"script fragment {token!r} missing from argv"


def test_bare_dash_s_is_never_used_again():
    argv, _ = _captured_exec()
    assert list(argv[-2:]) != ["bash", "-s"], "regressed to `bash -s`: stdin cannot carry both script and password"


def test_the_password_is_on_stdin_and_never_in_argv():
    argv, stdin = _captured_exec()
    assert SENTINEL_ANY.encode() in (stdin or b""), "password did not reach stdin"
    assert SENTINEL_ANY not in " ".join(argv), "password leaked into argv (/proc is world-readable)"


def test_remote_stderr_is_redacted_before_logging():
    # The remote shell echoes a failed command back on stderr. If that is logged
    # raw, a bug that misplaces the password puts it in syslog -- and, via the
    # host OneAgent, in production Grail. This is what happened 2026-08-27.
    secret = "NOT-A-REAL-SECRET-fixture-value-only"
    leaked = f"bash: line 1: {secret}: command not found"
    out = worker_seed._redact(leaked, secret)
    assert secret not in out, f"credential survived redaction: {out!r}"
    assert "<redacted>" in out
    assert "command not found" in out, "redaction destroyed the diagnostic"


def test_redact_is_safe_with_no_secret():
    assert "boom" in worker_seed._redact("boom", "")
    assert worker_seed._redact("", "x") == ""


def test_updated_no_restart_is_not_misread_as_updated():
    # SEED_UPDATED is a SUBSTRING of SEED_UPDATED_NO_RESTART, so classification
    # order decides the answer. Getting it wrong reports a rewrite-without-
    # restart as a full success -- the one case an operator must not miss.
    assert worker_seed.classify_seed_output("SEED_UPDATED_NO_RESTART") == "updated-no-restart"
    assert worker_seed.classify_seed_output("SEED_UPDATED") == "updated"


def test_restarted_stale_agent_classifies():
    assert worker_seed.classify_seed_output("SEED_RESTARTED_STALE_AGENT") == "restarted-stale-agent"


def test_script_never_dies_between_rewrite_and_its_token():
    # `set -e` plus a failing restart aborted before the token printed: the file
    # was rewritten, the caller saw "unknown", and the next tick found the
    # credential correct and never restarted -- permanently stuck.
    s = worker_seed.build_seed_script()
    assert "if sudo -n systemctl restart" in s, "restart is not guarded; set -e can abort before the token"
    assert "SEED_UPDATED_NO_RESTART" in s


def test_env_file_existence_check_is_privileged():
    # /home/ops is drwxr-x--- and the script runs as `ubuntu`, so a bare
    # `[ -f ]` reports "missing" on every worker and the script no-ops forever.
    s = worker_seed.build_seed_script()
    assert "sudo -n test -f" in s, "env-file check is unprivileged; it will always say missing"

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
    print("all worker_seed tests passed")
