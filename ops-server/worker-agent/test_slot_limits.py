"""Tests for per-slot resource limits (phase 2).

Pure logic — no Redis/Docker — so it runs anywhere.

  - pytest:     /home/ops/ops-venv/bin/python -m pytest worker-agent/test_slot_limits.py
  - standalone: cd ops-server && /home/ops/ops-venv/bin/python -m worker-agent.test_slot_limits

Contract: slots used to be started with no resource limits whatsoever, so a
single learner's runaway could take the whole worker down with it. Limits are
derived from measurement (amd001, 2026-08-12: 1.61 GiB committed, 2.2–3.1 GiB
transient peak, 0.127 vCPU average per full Kubernetes-101 session) and are
opt-in per worker so they can be verified against a real lab before rollout.

The two deliberate omissions are as load-bearing as the limits themselves:
  * no --cpus  — a hard CFS quota would throttle the k3d-creation burst and
                 slow every provision; shares arbitrate contention only.
  * no disk    — overlayfs on ext4 cannot do --storage-opt size=.
"""

import importlib

from . import config


def _reload(**env):
    """Reload config with a patched environment and return the module."""
    import os
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return importlib.reload(config)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _args(**env):
    cfg = _reload(**env)
    try:
        return cfg.slot_limit_args()
    finally:
        importlib.reload(config)   # restore process default


# ── off by default ───────────────────────────────────────────────────────────

def test_disabled_by_default_emits_no_flags():
    # Existing workers must behave EXACTLY as before until explicitly opted in.
    assert _args(WORKER_SLOT_LIMITS=None) == []


def test_explicit_zero_disables():
    assert _args(WORKER_SLOT_LIMITS="0") == []


def test_truthy_spellings_enable():
    for raw in ("1", "true", "TRUE", "yes"):
        assert _args(WORKER_SLOT_LIMITS=raw), raw


# ── the envelope ─────────────────────────────────────────────────────────────

def test_default_envelope_matches_measurement():
    args = _args(WORKER_SLOT_LIMITS="1")
    pairs = dict(zip(args[::2], args[1::2]))
    # 8 GiB. The history matters, because this number has been wrong twice in
    # the same direction: 3072m claimed to clear the 3.1 GiB transient peak of
    # the operator / DynaKube steps and sat just under it, so a healthy lab at
    # full stretch was OOM-killed by its own guard rail; 4096m then cleared
    # k8s-101 but sat *below* Astroshop's 6,320 MiB of declared pod limits, so
    # a correct Astroshop session was killed for being the size it is designed
    # to be. The daily pool is heterogeneous, so its cap has to clear the
    # heaviest training that can land on it, not the one we measured.
    #
    # Capacity is planned from units, never from this number — this is the
    # *limit* to the unit model's *request*.
    # slot_memory_cap_mb(4 units) — sized to the heaviest MEASURED training
    # (Astroshop, 7,158 MiB per session), not to the 6,320 MiB its pod limits
    # declare. Under the previous 8192 cap a correct session ran at 87% of it.
    assert pairs["--memory"] == "20480m"
    assert pairs["--memory-reservation"] == "2048m"
    assert pairs["--pids-limit"] == "4096"
    assert pairs["--cpu-shares"] == "1024"


def test_swap_is_disabled_by_matching_memory():
    # --memory-swap == --memory means "no swap". A swapping k3d cluster is a
    # worse outcome than a failed one: it degrades every neighbour too.
    args = _args(WORKER_SLOT_LIMITS="1", WORKER_SLOT_MEMORY_MB="4096")
    pairs = dict(zip(args[::2], args[1::2]))
    assert pairs["--memory"] == "4096m"
    assert pairs["--memory-swap"] == pairs["--memory"]


def test_no_cpu_quota_ever():
    # Guard the deliberate choice: --cpus would throttle provisioning bursts.
    args = _args(WORKER_SLOT_LIMITS="1")
    assert "--cpus" not in args
    assert "--cpu-quota" not in args
    assert "--cpu-period" not in args


def test_no_disk_quota_ever():
    # overlayfs-on-ext4 rejects --storage-opt size=; emitting it would make
    # every slot fail to start.
    assert "--storage-opt" not in _args(WORKER_SLOT_LIMITS="1")


# ── overrides and guards ─────────────────────────────────────────────────────

def test_memory_override_respected():
    # Bigger instances (r6a: 64 GiB) may want a roomier per-slot ceiling.
    args = _args(WORKER_SLOT_LIMITS="1", WORKER_SLOT_MEMORY_MB="6144")
    assert dict(zip(args[::2], args[1::2]))["--memory"] == "6144m"


def test_reservation_dropped_when_not_below_hard_cap():
    # A reservation >= the cap is meaningless to the kernel; emit nothing
    # rather than an incoherent pair.
    args = _args(WORKER_SLOT_LIMITS="1",
                 WORKER_SLOT_MEMORY_MB="2048",
                 WORKER_SLOT_MEMORY_RESERVATION_MB="2048")
    assert "--memory-reservation" not in args
    assert dict(zip(args[::2], args[1::2]))["--memory"] == "2048m"


def test_reservation_dropped_when_zero():
    args = _args(WORKER_SLOT_LIMITS="1", WORKER_SLOT_MEMORY_RESERVATION_MB="0")
    assert "--memory-reservation" not in args


def test_args_are_flat_docker_argv():
    # Consumed via *slot_limit_args() inside create_subprocess_exec — every
    # element must be a plain string, and flags must pair up.
    args = _args(WORKER_SLOT_LIMITS="1")
    assert all(isinstance(a, str) for a in args)
    assert len(args) % 2 == 0
    assert all(a.startswith("--") for a in args[::2])


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    print(f"{'FAILED' if failures else 'OK'} ({failures} failures)")
    sys.exit(1 if failures else 0)
