"""Test executor — mirrors the GitHub Actions integration-tests.yaml flow.

The repo's GHA workflow uses devcontainers/ci@v0.3 to:
  1. Build/pull the devcontainer image
  2. Run the container with the devcontainer.json runArgs + --env-file
  3. Execute postCreateCommand (sets up k3d cluster + DT operator + apps)
  4. Execute postStartCommand
  5. Execute the user runCmd: zsh .devcontainer/test/integration.sh

We replicate that with: ``docker run -d ... sleep infinity`` + per-step
``docker exec`` so each PR is tested the same way GHA tests it, while
streaming live output to Redis for the dashboard.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

import redis.asyncio as redis_async

from .config import (
    REPOS_DIR,
    LOGS_DIR,
    WORKDIR,
    DT_ENVIRONMENT,
    DT_OPERATOR_TOKEN,
    DT_INGEST_TOKEN,
    TEST_TIMEOUT,
    TEST_IMAGE,
    WORKER_ARCH,
    WORKER_ID,
    APP_PROXY_PORT_START,
    APP_PROXY_PORT_COUNT,
    K3D_LB_HTTP_PORT,
)

log = logging.getLogger("ops-worker-agent")


async def _alloc_app_port(redis_pool) -> int | None:
    """Pop a free app proxy port from this worker's pool in Redis."""
    if redis_pool is None:
        return None
    try:
        port = await redis_pool.lpop(f"worker:{WORKER_ID}:app_ports_free")
        return int(port) if port else None
    except Exception as e:
        log.warning("Failed to allocate app proxy port: %s", e)
        return None


async def _free_app_port(redis_pool, port: int | None) -> None:
    """Return an app proxy port to the worker's free pool."""
    if redis_pool is None or port is None:
        return
    try:
        await redis_pool.rpush(f"worker:{WORKER_ID}:app_ports_free", str(port))
    except Exception as e:
        log.warning("Failed to free app proxy port %s: %s", port, e)


async def execute_integration_test(job: dict, redis_pool=None) -> dict:
    """Run an integration test against the PR's branch (or main, for nightly).

    If ``redis_pool`` is given, live output is streamed to ``job:livelog:<id>``
    every 2s while the test runs so the dashboard can tail it.
    """
    repo      = job["repo"]                       # base repo, e.g. dynatrace-wwse/codespaces-framework
    head_repo = job.get("head_repo") or repo      # for fork PRs (head.repo.full_name), else same as base
    ref       = job.get("ref") or job.get("head_branch") or "main"
    repo_name = repo.split("/")[-1]
    job_id    = job["job_id"]
    log_file  = LOGS_DIR / f"{job_id}.log"

    # Per-job working dir so concurrent tests can't trample each other
    work_dir = WORKDIR / job_id
    repo_dir = work_dir / repo_name
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    log.info("Cloning %s @ %s for job %s", head_repo, ref, job_id)
    await _git_clone(head_repo, ref, repo_dir)
    await _make_world_writable(repo_dir)
    _write_env_file(repo_dir / ".devcontainer" / ".env")

    start_time = time.time()
    log.info("Running integration test for %s (arch=%s, ref=%s)", repo_name, WORKER_ARCH, ref)

    # Architecture: Sysbox isolates each test in its own kernel/network/filesystem
    # bubble so multiple tests can run on the same host without colliding on
    # ports, container names, or k3d cluster names.
    #
    #   Outer container (--runtime=sysbox-runc):  docker:25-dind
    #     └─ Inner dockerd (private to this Sysbox)
    #         └─ dt-enablement test container (--privileged, --network=host
    #            within the Sysbox)
    #             └─ k3d cluster (server + LB, all confined to inner dockerd)
    #
    # The host docker daemon never sees the k3d containers; only the outer
    # Sysbox container is visible from `docker ps` on the host.
    workspace = f"/workspaces/{repo_name}"
    env_file_inside = f"{workspace}/.devcontainer/.env"
    sb_name = f"sb-{job_id[-32:]}"
    inner_name = "dt"

    sections = []
    rc = 0
    timed_out = False
    failed_step = None
    deadline = start_time + TEST_TIMEOUT
    app_port: int | None = None

    try:
        # 1. Start outer Sysbox container running docker:25-dind
        app_port = await _alloc_app_port(redis_pool)
        run_cmd = [
            "docker", "run",
            "-d",
            "--runtime=sysbox-runc",
            "--name", sb_name,
            "-v", f"{repo_dir}:{workspace}",
        ]
        if app_port:
            run_cmd += ["-p", f"{app_port}:{K3D_LB_HTTP_PORT}"]
        run_cmd.append("docker:25-dind")
        proc = await asyncio.create_subprocess_exec(
            *run_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            sections.append(f"=== sysbox docker run failed (rc={proc.returncode}) ===\n{err.decode(errors='replace')}")
            rc = proc.returncode
            raise RuntimeError("sysbox container start failed")

        if app_port and redis_pool:
            await redis_pool.hset(f"job:running:{job_id}", "app_proxy_port", str(app_port))

        # 2. Wait for the Sysbox's inner dockerd to be ready
        await _wait_for_inner_docker(sb_name)

        # 3. Inject dt-enablement into the inner Sysbox dockerd.
        #    Pull once to the outer daemon (cached on host); then stream
        #    save→load so we never hit Docker Hub rate limits per container.
        outer_inspect = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", TEST_IMAGE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if await outer_inspect.wait() != 0:
            outer_pull = await asyncio.create_subprocess_exec(
                "docker", "pull", TEST_IMAGE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await outer_pull.wait()
        save_proc = await asyncio.create_subprocess_exec(
            "docker", "save", TEST_IMAGE,
            stdout=asyncio.subprocess.PIPE,
        )
        load_proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", sb_name, "docker", "load",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = await save_proc.stdout.read(65536)
                if not chunk:
                    break
                load_proc.stdin.write(chunk)
            load_proc.stdin.close()
        finally:
            await asyncio.gather(save_proc.wait(), load_proc.wait())

        # 4. Start the inner dt-enablement container (bind-mount workspace
        #    from the Sysbox's view, mount the inner docker.sock so k3d can
        #    operate inside the Sysbox).
        inner_run = [
            "docker", "exec", sb_name,
            "docker", "run", "-d",
            "--init", "--privileged", "--network=host",
            "--name", inner_name,
            "--env-file", env_file_inside,
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{workspace}:{workspace}",
            "-w", workspace,
            "-e", "GIT_CONFIG_COUNT=1",
            "-e", "GIT_CONFIG_KEY_0=safe.directory",
            "-e", "GIT_CONFIG_VALUE_0=*",
            TEST_IMAGE,
            "sleep", "infinity",
        ]
        proc = await asyncio.create_subprocess_exec(
            *inner_run, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            sections.append(f"=== inner docker run failed (rc={proc.returncode}) ===\n{err.decode(errors='replace')}")
            rc = proc.returncode
            raise RuntimeError("inner container start failed")

        # 5. Wait for vscode (inside dt) to have docker access. The dt-enablement
        #    entrypoint adds vscode to the docker group asynchronously after
        #    `docker run -d` returns; if we exec before that finishes, k3d
        #    fails with EACCES on docker.sock.
        await _wait_for_inner_dt_ready(sb_name, inner_name)

        # 6. Run each step via nested docker exec, streaming output to Redis.
        steps = [
            ("postCreateCommand", "./.devcontainer/post-create.sh"),
            ("postStartCommand",  "./.devcontainer/post-start.sh"),
            ("integrationTest",   "zsh .devcontainer/test/integration.sh"),
        ]
        livelog_key = f"job:livelog:{job_id}" if redis_pool is not None else None
        if livelog_key:
            await redis_pool.set(livelog_key, "", ex=3600)

        for label, script in steps:
            header = f"\n=== {label} ===\n"
            sections.append(header)
            if livelog_key:
                await redis_pool.append(livelog_key, header)
            remaining = max(60, int(deadline - time.time()))
            # docker exec <sysbox> docker exec <dt> bash -lc <script>
            exec_cmd = [
                "docker", "exec", sb_name,
                "docker", "exec",
                "-w", workspace,
                inner_name,
                "bash", "-lc", script,
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *exec_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                step_out = await _stream_to_redis(
                    proc, redis_pool, livelog_key, remaining
                )
                sections.append(step_out)
                if proc.returncode != 0:
                    rc = proc.returncode
                    failed_step = label
                    msg = f"\n=== {label} exited with rc={rc} — stopping ===\n"
                    sections.append(msg)
                    if livelog_key:
                        await redis_pool.append(livelog_key, msg)
                    break
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    rest = await proc.stdout.read()
                    sections.append(rest.decode(errors="replace"))
                except Exception:
                    pass
                sections.append(f"\n=== {label} timed out ===\n")
                rc = 124
                failed_step = label
                timed_out = True
                break
    except Exception as e:
        sections.append(f"\n=== executor error: {e} ===\n")
        if rc == 0:
            rc = 1
    finally:
        await _free_app_port(redis_pool, app_port)
        # 7. Always tear down the outer Sysbox container — everything inside
        #    (inner dockerd, dt-enablement, k3d cluster) goes with it.
        #    -v also removes anonymous volumes (docker:dind declares a VOLUME for
        #    its data dir; without -v those volumes accumulate on the host).
        try:
            kill_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-fv", sb_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(kill_proc.wait(), timeout=60)
        except Exception:
            pass
        # Safety net: purge any other leftover anonymous volumes from this or
        # previous runs that didn't get the -v treatment (e.g. after a crash).
        try:
            vol_proc = await asyncio.create_subprocess_exec(
                "docker", "volume", "prune", "-f",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(vol_proc.wait(), timeout=30)
        except Exception:
            pass

    duration = int(time.time() - start_time)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"=== JOB: {job_id} ===\n"
        f"=== REPO: {head_repo}@{ref} (base: {repo}) | ARCH: {WORKER_ARCH} ===\n"
        f"=== DURATION: {duration}s | EXIT: {rc} | TIMED_OUT: {timed_out} ===\n"
    )
    log_file.write_text(_mask_secrets(header + "".join(sections)))

    # No host-level cleanup needed — Sysbox container teardown above already
    # took the inner dockerd, dt-enablement, and k3d cluster with it.
    shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "test": "integration",
        "arch": WORKER_ARCH,
        "ref": ref,
        "exit_code": rc,
        "duration_seconds": duration,
        "passed": rc == 0,
        "timed_out": timed_out,
        "failed_step": failed_step or "",
        "log_file": str(log_file),
    }


async def execute_daemon(job: dict, redis_pool=None) -> dict:
    """Start the devcontainer environment and keep it alive until terminated.

    Mirrors execute_integration_test setup (clone → Sysbox → dt → postCreate →
    postStart) but skips the integration test and blocks on ``docker wait``
    instead, giving users an interactive shell for training / exploration.
    """
    repo      = job["repo"]
    head_repo = job.get("head_repo") or repo
    ref       = job.get("ref") or job.get("head_branch") or "main"
    repo_name = repo.split("/")[-1]
    job_id    = job["job_id"]
    log_file  = LOGS_DIR / f"{job_id}.log"

    work_dir = WORKDIR / job_id
    repo_dir = work_dir / repo_name
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    log.info("Cloning %s @ %s for daemon %s", head_repo, ref, job_id)
    await _git_clone(head_repo, ref, repo_dir)
    await _make_world_writable(repo_dir)
    _write_env_file(repo_dir / ".devcontainer" / ".env")

    start_time = time.time()
    workspace = f"/workspaces/{repo_name}"
    env_file_inside = f"{workspace}/.devcontainer/.env"
    sb_name = f"sb-{job_id[-32:]}"
    inner_name = "dt"

    sections = []
    rc = 0
    livelog_key = f"job:livelog:{job_id}" if redis_pool is not None else None
    app_port: int | None = None

    try:
        # 1. Outer Sysbox container
        app_port = await _alloc_app_port(redis_pool)
        run_cmd = [
            "docker", "run", "-d",
            "--runtime=sysbox-runc",
            "--name", sb_name,
            "-v", f"{repo_dir}:{workspace}",
        ]
        if app_port:
            run_cmd += ["-p", f"{app_port}:{K3D_LB_HTTP_PORT}"]
        run_cmd.append("docker:25-dind")
        proc = await asyncio.create_subprocess_exec(
            *run_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            sections.append(f"=== sysbox docker run failed (rc={proc.returncode}) ===\n{err.decode(errors='replace')}")
            raise RuntimeError("sysbox container start failed")

        if app_port and redis_pool:
            await redis_pool.hset(f"job:running:{job_id}", "app_proxy_port", str(app_port))

        await _wait_for_inner_docker(sb_name)

        # 2. Pull + start dt container
        pull = await asyncio.create_subprocess_exec(
            "docker", "exec", sb_name, "docker", "pull", TEST_IMAGE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await pull.wait()

        inner_run = [
            "docker", "exec", sb_name,
            "docker", "run", "-d",
            "--init", "--privileged", "--network=host",
            "--name", inner_name,
            "--env-file", env_file_inside,
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{workspace}:{workspace}",
            "-w", workspace,
            "-e", "GIT_CONFIG_COUNT=1",
            "-e", "GIT_CONFIG_KEY_0=safe.directory",
            "-e", "GIT_CONFIG_VALUE_0=*",
            TEST_IMAGE, "sleep", "infinity",
        ]
        proc = await asyncio.create_subprocess_exec(
            *inner_run, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            sections.append(f"=== inner docker run failed (rc={proc.returncode}) ===\n{err.decode(errors='replace')}")
            raise RuntimeError("inner container start failed")

        await _wait_for_inner_dt_ready(sb_name, inner_name)

        # 3. postCreate + postStart (set up cluster and apps)
        if livelog_key:
            await redis_pool.set(livelog_key, "", ex=86400)

        for label, script in [
            ("postCreateCommand", "./.devcontainer/post-create.sh"),
            ("postStartCommand",  "./.devcontainer/post-start.sh"),
        ]:
            header = f"\n=== {label} ===\n"
            sections.append(header)
            if livelog_key:
                await redis_pool.append(livelog_key, header)
            exec_cmd = [
                "docker", "exec", sb_name,
                "docker", "exec", "-w", workspace, inner_name,
                "bash", "-lc", script,
            ]
            proc = await asyncio.create_subprocess_exec(
                *exec_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            step_out = await _stream_to_redis(proc, redis_pool, livelog_key, timeout_s=1800)
            sections.append(step_out)
            if proc.returncode != 0:
                rc = proc.returncode
                msg = f"\n=== {label} failed (rc={rc}) — daemon environment may be incomplete ===\n"
                sections.append(msg)
                if livelog_key:
                    await redis_pool.append(livelog_key, msg)

        # 4. Signal readiness then block until the container is terminated
        ready_msg = (
            "\n=== Daemon ready — environment is up ===\n"
            "=== Connect via the Shell button. Terminate when done. ===\n"
        )
        sections.append(ready_msg)
        if livelog_key:
            await redis_pool.append(livelog_key, ready_msg)
            await redis_pool.expire(livelog_key, 86400)

        log.info("Daemon %s ready — waiting for termination", job_id)
        wait_proc = await asyncio.create_subprocess_exec(
            "docker", "wait", sb_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await wait_proc.wait()

    except Exception as e:
        sections.append(f"\n=== daemon error: {e} ===\n")
        if rc == 0:
            rc = 1
    finally:
        await _free_app_port(redis_pool, app_port)
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            kill_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-fv", sb_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(kill_proc.wait(), timeout=60)
        except Exception:
            pass

    duration = int(time.time() - start_time)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"=== DAEMON: {job_id} ===\n"
        f"=== REPO: {head_repo}@{ref} | ARCH: {WORKER_ARCH} ===\n"
        f"=== DURATION: {duration}s ===\n"
    )
    log_file.write_text(_mask_secrets(header + "".join(sections)))

    return {
        "test": "daemon",
        "arch": WORKER_ARCH,
        "ref": ref,
        "exit_code": rc,
        "duration_seconds": duration,
        "passed": True,   # termination is expected/normal
        "timed_out": False,
        "failed_step": "",
        "log_file": str(log_file),
    }


async def _stream_to_redis(proc, redis_pool, livelog_key, timeout_s: int) -> str:
    """Read proc.stdout line-by-line and append to ``livelog_key`` every ~1s.

    Returns the full captured output. Caps the live key at 256KB by trimming
    to the tail when the buffer grows; the on-disk log keeps the full thing.
    """
    full = []
    pending = []
    last_flush = time.time()
    deadline = last_flush + timeout_s
    MAX_LIVE_BYTES = 256 * 1024

    async def flush():
        if not pending or redis_pool is None or livelog_key is None:
            return
        chunk = "".join(pending)
        pending.clear()
        try:
            await redis_pool.append(livelog_key, _mask_secrets(chunk))
            # Trim if it grew past max
            current = await redis_pool.strlen(livelog_key)
            if current and current > MAX_LIVE_BYTES:
                tail = await redis_pool.getrange(livelog_key, current - MAX_LIVE_BYTES, current)
                await redis_pool.set(livelog_key, tail, ex=3600)
        except Exception as e:
            log.warning("livelog flush failed: %s", e)

    while True:
        if time.time() > deadline:
            await flush()
            raise asyncio.TimeoutError()
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
        except asyncio.TimeoutError:
            line = b""
        if not line:
            if proc.returncode is not None or proc.stdout.at_eof():
                break
            # No data this tick — flush what we have
            if time.time() - last_flush > 1.0:
                await flush()
                last_flush = time.time()
            continue
        decoded = line.decode(errors="replace")
        full.append(decoded)
        pending.append(decoded)
        if time.time() - last_flush > 1.0:
            await flush()
            last_flush = time.time()

    await proc.wait()
    await flush()
    return "".join(full)


async def _wait_for_inner_docker(sb_name: str, timeout_s: int = 60):
    """Wait until the Sysbox container's inner dockerd is responsive."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", sb_name,
            "docker", "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if (await proc.wait()) == 0:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"inner dockerd never came up in {sb_name}")


async def _wait_for_inner_dt_ready(sb_name: str, inner_name: str, timeout_s: int = 60):
    """Wait until vscode inside the dt-enablement container can talk to docker."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", sb_name,
            "docker", "exec", inner_name,
            "sh", "-c", "docker info >/dev/null 2>&1",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if (await proc.wait()) == 0:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"vscode never got docker access in {sb_name}/{inner_name}")


def _mask_secrets(content: str) -> str:
    """Redact known DT tokens before writing/uploading the log.

    The dt-enablement image's entrypoint dumps the environment via ``set``
    for debugging after switching to the docker group, which leaks tokens
    if not masked. We also mask any literal token value found anywhere
    in stdout/stderr (e.g. helm install args, kubectl secrets dumps).
    """
    import re
    # Mask each known token value verbatim
    for secret in (DT_OPERATOR_TOKEN, DT_INGEST_TOKEN):
        if secret and len(secret) > 12:
            content = content.replace(secret, _redact(secret))
    # Catch-all for any dt0c01.*/dt0s01.*/dt0s16.* token shape we missed
    # (60+ chars, alphanumeric + dots/underscores)
    content = re.sub(
        r"\bdt0[cs]\d{2}\.[A-Z0-9]{24}\.[A-Z0-9]{60,80}\b",
        lambda m: _redact(m.group(0)),
        content,
    )
    return content


def _redact(token: str) -> str:
    """Keep the prefix (dt0c01.XXXXXXXX) so we can still tell which token
    is which; replace the secret part with stars."""
    if not token:
        return "***"
    # Show first 14 chars (dt0c01.XXXXXXXX) then mask the rest
    return token[:14] + "***REDACTED***"


def _write_env_file(env_path: Path):
    """Mirror what the GHA workflow writes to .devcontainer/.env."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        f"DT_ENVIRONMENT={DT_ENVIRONMENT}\n"
        f"DT_OPERATOR_TOKEN={DT_OPERATOR_TOKEN}\n"
        f"DT_INGEST_TOKEN={DT_INGEST_TOKEN}\n"
    )


async def _make_world_writable(repo_dir: Path):
    """Widen perms so the container's vscode user (uid 1000) can write a clone owned by ops (uid 1001)."""
    proc = await asyncio.create_subprocess_exec(
        "chmod", "-R", "go+rwX", str(repo_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _git_clone(repo: str, ref: str, dest: Path):
    """Shallow-clone the given repo at the given ref (branch or tag) into ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    # --branch accepts branches and tags. Falls back to default branch if ref doesn't exist.
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--branch", ref, url, str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Retry without --branch in case ref is a sha (rare for our workflow).
        log.warning("git clone --branch %s failed; retrying default branch", ref)
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", url, str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone {url} failed (rc={proc.returncode}): {stderr.decode()[:500]}"
            )


async def _cleanup_clusters():
    """Wipe stale clusters / containers / kubeconfig before or after a test.

    Failures are ignored — best-effort cleanup. The next test will pick up
    a clean slate either way.
    """
    cmds = [
        # Remove all k3d clusters (most common state from a previous test)
        ["bash", "-c", "k3d cluster list -o name 2>/dev/null | xargs -r -I{} k3d cluster delete {}"],
        # Remove all kind clusters (if framework was switched to kind engine)
        ["bash", "-c", "kind get clusters 2>/dev/null | xargs -r -I{} kind delete cluster --name {}"],
        # Force-remove any framework dev container with a known name
        ["bash", "-c", "docker rm -f dt-enablement 2>/dev/null || true"],
        # Stale rancher/k3s containers (k3d nodes that didn't get cleaned)
        ["bash", "-c", "docker ps -aq --filter 'ancestor=rancher/k3s' | xargs -r docker rm -f 2>/dev/null || true"],
        ["bash", "-c", "docker ps -aq --filter 'name=k3d-' | xargs -r docker rm -f 2>/dev/null || true"],
        # Stale kubeconfig — k3d/kind write here; next post-create.sh will rewrite cleanly
        ["bash", "-c", "rm -f ~/.kube/config 2>/dev/null || true"],
    ]
    for cmd in cmds:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception:
            pass
