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

## Warm Sysbox Pool

When a ``SysboxSlot`` is passed, the outer Sysbox container is already
running with its inner dockerd ready and TEST_IMAGE pre-loaded. The
executor skips those startup steps entirely (saves 60-120s) and goes
straight to git clone → start inner container → run steps.

The slot's Sysbox is NOT torn down on job completion; the pool's
``release()`` method cleans inner state (rm dt, prune volumes/networks)
and returns the slot to the queue for the next job.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as redis_async

from .config import (
    REPOS_DIR,
    LOGS_DIR,
    WORKDIR,
    DT_ENVIRONMENT,
    DT_OPERATOR_TOKEN,
    DT_INGEST_TOKEN,
    DT_LLM_TOKEN,
    TEST_TIMEOUT,
    TEST_IMAGE,
    WORKER_ARCH,
    WORKER_ID,
    APP_PROXY_PORT_START,
    APP_PROXY_PORT_COUNT,
    K3D_LB_HTTP_PORT,
)

log = logging.getLogger("ops-worker-agent")


@dataclass
class SysboxSlot:
    """A pre-warmed Sysbox container managed by SysboxPool.

    The outer Sysbox (docker:25-dind) is started once at agent startup with
    its workspace directory mounted at /workspaces and TEST_IMAGE pre-loaded
    into the inner dockerd. Jobs claim a slot, clone their repo into
    slot.workspace, run, then release the slot — the outer container stays
    alive and the inner docker is wiped between uses.
    """
    index: int
    sb_name: str              # docker container name, e.g. sb-slot-abc123-0
    workspace: Path           # host path mounted at /workspaces inside the Sysbox
    port: int                 # fixed host port published on this Sysbox for app proxy
    image_digest: str = field(default="", compare=False)


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


async def execute_integration_test(
    job: dict,
    redis_pool=None,
    slot: SysboxSlot | None = None,
) -> dict:
    """Run an integration test against the PR's branch (or main, for nightly).

    If ``slot`` is provided the outer Sysbox startup and image loading are
    skipped — the slot's container is already warm. Otherwise falls back to
    the traditional per-job Sysbox lifecycle.

    If ``redis_pool`` is given, live output is streamed to ``job:livelog:<id>``
    every 2s while the test runs so the dashboard can tail it.
    """
    repo      = job["repo"]
    head_repo = job.get("head_repo") or repo
    ref       = job.get("ref") or job.get("head_branch") or "main"
    repo_name = repo.split("/")[-1]
    job_id    = job["job_id"]
    log_file  = LOGS_DIR / f"{job_id}.log"

    if slot:
        # Clone into the slot's persistent workspace directory.
        repo_dir = slot.workspace / repo_name
        # Clean via docker exec so container-owned files (uid 1000) are removable.
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", slot.sb_name, "sh", "-c",
            f"rm -rf /workspaces/{repo_name}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.mkdir(parents=True, exist_ok=True)
        work_dir = None
    else:
        # Legacy path: per-job working directory.
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

    workspace = f"/workspaces/{repo_name}"
    env_file_inside = f"{workspace}/.devcontainer/.env"
    sb_name = slot.sb_name if slot else f"sb-{job_id[-32:]}"
    inner_name = "dt"

    sections = []
    rc = 0
    timed_out = False
    failed_step = None
    deadline = start_time + TEST_TIMEOUT
    app_port: int | None = slot.port if slot else None

    try:
        livelog_key = f"job:livelog:{job_id}" if redis_pool is not None else None
        if livelog_key:
            warm_note = f"slot {slot.index} pre-warmed" if slot else "starting Sysbox..."
            await redis_pool.set(
                livelog_key,
                f"=== JOB: {job_id} ===\n"
                f"=== REPO: {head_repo}@{ref} | ARCH: {WORKER_ARCH} ===\n"
                f"\n[setup] {warm_note}\n",
                ex=3600,
            )

        async def _setup_log(msg: str) -> None:
            if livelog_key and redis_pool is not None:
                ts = time.strftime("%H:%M:%S", time.gmtime())
                await redis_pool.append(livelog_key, f"[{ts}] [setup] {msg}\n")

        if slot:
            # Slot already has the Sysbox running with inner dockerd and image ready.
            await _setup_log(f"Slot {slot.index} ({slot.sb_name}) — inner Docker and image pre-warmed")
            if redis_pool:
                await redis_pool.hset(f"job:running:{job_id}", "app_proxy_port", str(slot.port))
        else:
            # ── Legacy: start a fresh Sysbox for this job ──────────────────
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

            await _setup_log("Sysbox container started — waiting for inner Docker daemon...")
            await _wait_for_inner_docker(sb_name)

            await _setup_log("Inner Docker ready — loading test image into Sysbox...")
            outer_inspect = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", TEST_IMAGE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            if await outer_inspect.wait() != 0:
                await _setup_log("Pulling test image from registry (first run — cached after this)...")
                outer_pull = await asyncio.create_subprocess_exec(
                    "docker", "pull", TEST_IMAGE,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await outer_pull.wait()
            await _pipe_image_to_sysbox(sb_name)

        await _setup_log("Starting test container...")
        # Inner container: bind-mount the workspace path that's already visible
        # inside the Sysbox (whether via slot mount or per-job volume mount).
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
            "-e", "ORBITAL_ENVIRONMENT=true",
            "-e", f"ORBITAL_JOB_ID={job_id}",
            "-e", f"ARCH={WORKER_ARCH}",
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

        await _setup_log("Container started — waiting for docker group membership...")
        await _wait_for_inner_dt_ready(sb_name, inner_name)

        await _setup_log("Environment ready — starting test steps")
        custom_script = job.get("test_script") or "zsh .devcontainer/test/integration.sh"
        test_label = f"frameworkTest:{job['suite']}" if job.get("suite") else "integrationTest"
        steps = [
            ("postCreateCommand", "./.devcontainer/post-create.sh"),
            ("postStartCommand",  "./.devcontainer/post-start.sh"),
            (test_label,          custom_script),
        ]

        for label, script in steps:
            header = f"\n=== {label} ===\n"
            sections.append(header)
            if livelog_key:
                await redis_pool.append(livelog_key, header)
            remaining = max(60, int(deadline - time.time()))
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
                step_out = await _stream_to_redis(proc, redis_pool, livelog_key, remaining)
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
        if slot:
            # Slot path: only clean the repo clone. The pool's release() handles
            # inner container removal and Sysbox state reset.
            shutil.rmtree(repo_dir, ignore_errors=True)
        else:
            # Legacy path: tear down the entire outer Sysbox (takes everything with it).
            await _free_app_port(redis_pool, app_port)
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-fv", sb_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(kill_proc.wait(), timeout=60)
            except Exception:
                pass
            # Volumes then networks. k3d creates a `k3d-<cluster>` docker network
            # per cluster; without Sysbox isolation these accumulate on the host
            # and clutter the worker. Prune regardless of exit status.
            for prune_args in (
                ["docker", "volume", "prune", "-f"],
                ["docker", "network", "prune", "-f"],
            ):
                try:
                    prune_proc = await asyncio.create_subprocess_exec(
                        *prune_args,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(prune_proc.wait(), timeout=30)
                except Exception:
                    pass
            shutil.rmtree(work_dir, ignore_errors=True)

    duration = int(time.time() - start_time)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"=== JOB: {job_id} ===\n"
        f"=== REPO: {head_repo}@{ref} (base: {repo}) | ARCH: {WORKER_ARCH} ===\n"
        f"=== DURATION: {duration}s | EXIT: {rc} | TIMED_OUT: {timed_out} ===\n"
    )
    log_file.write_text(_mask_secrets(header + "".join(sections)))

    if redis_pool and job.get("framework_suite"):
        suite = job["framework_suite"]
        import json as _json
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        log_tail = "".join(sections)[-4000:]
        await redis_pool.hset(f"framework:suite:{suite}:last", mapping={
            "job_id": job_id, "timestamp": now,
            "exit_code": str(rc), "passed": "true" if rc == 0 else "false",
            "duration_s": str(duration), "arch": WORKER_ARCH, "log_tail": log_tail,
        })
        run_entry = _json.dumps({"suite": suite, "job_id": job_id, "timestamp": now,
                                  "passed": rc == 0, "arch": WORKER_ARCH, "duration_s": duration})
        await redis_pool.lpush("framework:runs", run_entry)
        await redis_pool.ltrim("framework:runs", 0, 99)

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


async def execute_daemon(
    job: dict,
    redis_pool=None,
    slot: SysboxSlot | None = None,
) -> dict:
    """Start the devcontainer environment and keep it alive until terminated.

    Mirrors execute_integration_test setup (clone → Sysbox → dt → postCreate →
    postStart) but skips the integration test and blocks on ``docker wait``
    instead, giving users an interactive shell for training / exploration.

    When ``slot`` is provided the Sysbox startup and image-load are skipped.
    """
    repo      = job["repo"]
    head_repo = job.get("head_repo") or repo
    ref       = job.get("ref") or job.get("head_branch") or "main"
    repo_name = repo.split("/")[-1]
    job_id    = job["job_id"]
    log_file  = LOGS_DIR / f"{job_id}.log"

    if slot:
        repo_dir = slot.workspace / repo_name
        # Clean via docker exec so container-owned files (uid 1000) are removable.
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", slot.sb_name, "sh", "-c",
            f"rm -rf /workspaces/{repo_name}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.mkdir(parents=True, exist_ok=True)
        work_dir = None
    else:
        work_dir = WORKDIR / job_id
        repo_dir = work_dir / repo_name
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)

    log.info("Cloning %s @ %s for daemon %s", head_repo, ref, job_id)
    await _git_clone(head_repo, ref, repo_dir)
    await _make_world_writable(repo_dir)
    # Per-session provisioned tokens override the worker-level static creds.
    dt_env_overrides: dict[str, str] | None = job.get("dt_env") or None
    _write_env_file(
        repo_dir / ".devcontainer" / ".env",
        overrides=dt_env_overrides,
        orbital_job_id=job_id,
    )

    start_time = time.time()
    workspace = f"/workspaces/{repo_name}"
    env_file_inside = f"{workspace}/.devcontainer/.env"
    sb_name = slot.sb_name if slot else f"sb-{job_id[-32:]}"
    inner_name = "dt"

    sections = []
    rc = 0
    livelog_key = f"job:livelog:{job_id}" if redis_pool is not None else None

    try:
        if slot:
            if redis_pool:
                await redis_pool.hset(f"job:running:{job_id}", "app_proxy_port", str(slot.port))
            log.info("Daemon %s using pre-warmed slot %d (%s)", job_id, slot.index, slot.sb_name)
        else:
            # Legacy: start fresh Sysbox, load image via save/load (not docker pull —
            # pull hits the registry every time; save/load reuses the outer daemon cache).
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

            # Load image via save→load pipe so we reuse the outer daemon's cached layer.
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
            await _pipe_image_to_sysbox(sb_name)

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
            "-e", "ORBITAL_ENVIRONMENT=true",
            "-e", f"ORBITAL_JOB_ID={job_id}",
            "-e", f"ARCH={WORKER_ARCH}",
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
        if slot:
            # Slot path: clean repo clone only; pool.release() handles the rest.
            shutil.rmtree(repo_dir, ignore_errors=True)
        else:
            await _free_app_port(redis_pool, locals().get("app_port"))
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
        "passed": True,
        "timed_out": False,
        "failed_step": "",
        "log_file": str(log_file),
    }


async def _pipe_image_to_sysbox(sb_name: str) -> None:
    """Stream TEST_IMAGE from the outer daemon into the Sysbox's inner docker via save→load."""
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
    """Redact known DT tokens before writing/uploading the log."""
    import re
    for secret in (DT_OPERATOR_TOKEN, DT_INGEST_TOKEN):
        if secret and len(secret) > 12:
            content = content.replace(secret, _redact(secret))
    content = re.sub(
        r"\bdt0[cs]\d{2}\.[A-Z0-9]{24}\.[A-Z0-9]{60,80}\b",
        lambda m: _redact(m.group(0)),
        content,
    )
    return content


def _redact(token: str) -> str:
    if not token:
        return "***"
    return token[:14] + "***REDACTED***"


def _write_env_file(
    env_path: Path,
    overrides: dict[str, str] | None = None,
    orbital_job_id: str = "",
):
    """Write .devcontainer/.env with DT credentials.

    Uses per-session overrides (provisioned tokens) when provided, falls back
    to the worker-level static env vars (used by integration tests).
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    resolved = {
        "DT_ENVIRONMENT":    DT_ENVIRONMENT,
        "DT_OPERATOR_TOKEN": DT_OPERATOR_TOKEN,
        "DT_INGEST_TOKEN":   DT_INGEST_TOKEN,
        "DT_LLM_TOKEN":      DT_LLM_TOKEN,
    }
    if orbital_job_id:
        resolved["ORBITAL_JOB_ID"] = orbital_job_id
    if overrides:
        resolved.update(overrides)
    env_path.write_text("".join(f"{k}={v}\n" for k, v in resolved.items() if v))


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
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--branch", ref, url, str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
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
