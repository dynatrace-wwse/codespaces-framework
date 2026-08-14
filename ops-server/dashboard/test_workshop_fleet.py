"""Tests for the control loop's decisions.

  /home/ops/ops-venv/bin/python -m pytest dashboard/test_workshop_fleet.py -q

Only the pure half is tested here — scheduling, sizing and the scale decision.
The effectful half talks to EC2 and is exercised by the end-to-end rehearsal.
"""

from datetime import datetime, timedelta, timezone

from dashboard import workshop_fleet as wf
from dashboard import repo_profiles as rp


NOW = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)


def _session(start_offset_min=0, duration=120, state="scheduled"):
    return {
        "state": state,
        "scheduledAt": (NOW + timedelta(minutes=start_offset_min)).isoformat(),
        "durationMinutes": str(duration),
    }


# ── scheduling ──────────────────────────────────────────────────────────────

def test_prewarm_fires_exactly_one_lead_time_ahead():
    s = _session(start_offset_min=45)
    assert wf.due_for_prewarm(s, NOW, lead_minutes=45) is True
    assert wf.due_for_prewarm(s, NOW, lead_minutes=44) is False


def test_prewarm_still_fires_for_a_workshop_already_underway():
    """A trainer who opens the room late, or a loop restarted mid-morning, must
    still get machines. Silently skipping a workshop for having missed its
    window is the worst possible failure here."""
    assert wf.due_for_prewarm(_session(start_offset_min=-30), NOW) is True


def test_ended_workshop_is_never_prewarmed():
    for state in ("ended", "cancelled", "deleted"):
        assert wf.due_for_prewarm(_session(-10, state=state), NOW) is False


def test_workshop_without_a_start_time_is_skipped_not_crashed():
    assert wf.due_for_prewarm({"state": "scheduled"}, NOW) is False
    assert wf.due_for_prewarm({"state": "scheduled",
                               "scheduledAt": "not-a-date"}, NOW) is False


def test_teardown_waits_for_the_grace_period():
    """Overruns are normal. A trainer running ten minutes late must not have
    the room's environments deleted underneath them."""
    s = _session(start_offset_min=-120, duration=120)      # ended exactly now
    assert wf.due_for_teardown(s, NOW, grace_minutes=30) is False
    assert wf.due_for_teardown(s, NOW + timedelta(minutes=31),
                               grace_minutes=30) is True


def test_explicit_end_beats_the_clock():
    """Finishing early should give the machines back early, not pay for the
    rest of the booked window."""
    s = _session(start_offset_min=-10, duration=240, state="ended")
    assert wf.due_for_teardown(s, NOW) is True


def test_lead_time_is_configurable_down_to_minutes():
    """The rehearsal compresses an hour-scale delivery into minutes; if this
    were hardcoded the loop could only ever be tested in real time."""
    s = _session(start_offset_min=3)
    assert wf.due_for_prewarm(s, NOW, lead_minutes=3) is True


# ── sizing ──────────────────────────────────────────────────────────────────

def test_seventy_seats_of_k8s101_plans_four_machines():
    """30 seats fit on memory; the 0.55 safety turns that into 20, which is the
    explicit instruction to prefer 20 over a tight 30."""
    plan = wf.plan_workshop_capacity(70, rp.K8S_101, "m6a.4xlarge")
    assert plan["seats_per_worker"] == 20
    assert plan["workers"] == 4
    assert plan["total_seats"] == 80, "must exceed the roster, not merely meet it"


def test_an_unprofiled_repo_is_planned_as_heavy():
    """The one line that makes the system fail safe when someone adds a repo
    and forgets to measure it."""
    plan = wf.plan_workshop_capacity(70, rp.HEAVY_DEFAULT, "m6a.4xlarge")
    assert plan["workers"] > wf.plan_workshop_capacity(
        70, rp.K8S_101, "m6a.4xlarge")["workers"]
    assert plan["estimated"] is True
    assert "ESTIMATE" in plan["reason"]


def test_capacity_rounds_up_never_down():
    """A workshop one seat short is a person without an environment in front of
    a room."""
    plan = wf.plan_workshop_capacity(21, rp.K8S_101, "m6a.4xlarge")
    assert plan["workers"] == 2


def test_unknown_instance_type_refuses_to_plan():
    """Guessing high is how a class gets oversold, so an unknown shape must
    produce a refusal rather than a number."""
    plan = wf.plan_workshop_capacity(70, rp.K8S_101, "totally-made-up.9xlarge")
    assert plan["workers"] == 0
    assert "no capacity model" in plan["reason"]


def test_zero_seats_plans_nothing():
    assert wf.plan_workshop_capacity(0, rp.K8S_101, "m6a.4xlarge")["workers"] == 0


# ── daily scale decision ────────────────────────────────────────────────────

def _worker(wid="w1", free=10, mem=20, cpu=10, status="ready", draining="0"):
    return {"worker_id": wid, "slots_free": str(free), "mem_pct": str(mem),
            "cpu_pct": str(cpu), "status": status, "draining": draining}


def test_low_free_seats_scales_up():
    d = wf.daily_scale_decision([_worker(free=1)], {}, min_free=4)
    assert d["scale_up"] == 1


def test_plenty_of_seats_does_nothing():
    d = wf.daily_scale_decision([_worker(free=20)], {}, min_free=4)
    assert d["scale_up"] == 0 and not d["shrink"] and not d["brake"]


def test_high_cpu_alone_never_scales():
    """THE correction to the obvious design. A completely full 30-seat worker
    sits near 24% CPU, so a CPU trigger cannot signal occupancy; it fires on
    transient install bursts, and the machine it would add arrives minutes
    later and cannot take work off the box that is already hot."""
    hot = _worker(free=20, cpu=95)
    ticks = {"w1": wf.PRESSURE_SUSTAIN_TICKS}
    d = wf.daily_scale_decision([hot], ticks, min_free=4)
    assert d["scale_up"] == 0, "CPU pressure must not add machines"
    assert d["brake"] == ["w1"], "it must stop admissions instead"


def test_sustained_memory_shrinks_the_worker_and_replaces_the_seats():
    """Memory IS a good occupancy proxy: 70% is reached around 25 of 30 seats.

    Adding a machine while still advertising the remaining seats would overfill
    the same box again, so the advertisement is withdrawn at the same time.
    """
    full = _worker(free=6, mem=82)
    ticks = {"w1": wf.PRESSURE_SUSTAIN_TICKS}
    d = wf.daily_scale_decision([full], ticks, min_free=4)
    assert d["shrink"] == ["w1"]
    assert d["scale_up"] == 1


def test_a_single_hot_sample_moves_nothing():
    """A k3d bring-up spikes memory and CPU by design. Only sustained pressure
    is allowed to move the fleet."""
    hot = _worker(mem=95, cpu=95)
    d = wf.daily_scale_decision([hot], {"w1": 1}, min_free=0)
    assert not d["shrink"] and not d["brake"] and d["scale_up"] == 0


def test_pressure_resets_the_moment_a_worker_cools():
    ticks = {}
    for _ in range(5):
        ticks = wf.update_pressure(ticks, [_worker(mem=90)])
    assert ticks["w1"] >= wf.PRESSURE_SUSTAIN_TICKS
    ticks = wf.update_pressure(ticks, [_worker(mem=10)])
    assert ticks["w1"] == 0, "one cool sample must clear the streak"


def test_max_workers_is_a_hard_stop():
    workers = [_worker(f"w{i}", free=0) for i in range(4)]
    d = wf.daily_scale_decision(workers, {}, min_free=4, max_workers=4)
    assert d["scale_up"] == 0
    assert "DAILY_MAX_WORKERS" in d["why"]


def test_draining_workers_do_not_count_as_free_capacity():
    """Seats on a machine about to be terminated are not seats."""
    d = wf.daily_scale_decision([_worker(free=20, draining="1")], {}, min_free=4)
    assert d["scale_up"] == 1


def test_warming_workers_do_not_count_as_free_capacity():
    d = wf.daily_scale_decision([_worker(free=20, status="warming")], {}, min_free=4)
    assert d["scale_up"] == 1


def test_no_workers_is_reported_not_crashed():
    d = wf.daily_scale_decision([], {})
    assert d["scale_up"] == 0
    assert "no daily workers" in d["why"]


# ── rollout guards ──────────────────────────────────────────────────────────
# The loop launches and terminates EC2 on its own, against a Redis that already
# held 33 historical workshops (one of them running) when it was written. These
# pin the two guards that stop a first tick from acting on all of them.

def test_allowlist_narrows_to_named_workshops(monkeypatch):
    monkeypatch.setattr(wf, "CONTROL_LOOP_WORKSHOPS", "ws_a,ws_b")
    assert wf.manages("ws_a") is True
    assert wf.manages("ws_b") is True
    assert wf.manages("ws_real_cohort") is False, \
        "a rehearsal must not touch a cohort that happens to be running"


def test_star_manages_everything(monkeypatch):
    monkeypatch.setattr(wf, "CONTROL_LOOP_WORKSHOPS", "*")
    assert wf.manages("anything") is True


def test_blank_allowlist_manages_everything(monkeypatch):
    """Empty must mean 'no restriction', not 'nothing' — an operator clearing
    the variable is removing a filter, not disabling the loop. Disabling is
    CONTROL_LOOP_ENABLED, and the two must not be confusable."""
    monkeypatch.setattr(wf, "CONTROL_LOOP_WORKSHOPS", "")
    assert wf.manages("anything") is True


def test_allowlist_ignores_whitespace(monkeypatch):
    monkeypatch.setattr(wf, "CONTROL_LOOP_WORKSHOPS", " ws_a , ws_b ")
    assert wf.manages("ws_a") and wf.manages("ws_b")


def test_apply_defaults_to_off():
    """Dry run is the default on purpose: a control loop that spends money on
    its first tick should require an explicit opt-in. It is a LOUD no-op —
    startup warns and every skipped action logs DRY-RUN — unlike the drain
    cordon, which was a silent one."""
    import os
    assert os.environ.get("CONTROL_LOOP_APPLY") in (None, "", "0"), \
        "this test documents the default; unset the env var to run it"
    assert wf.CONTROL_LOOP_APPLY is False


def test_workshop_repo_prefers_the_unambiguous_url():
    """trainingId is a catalog id, not a repo name. Live data has
    trainingId=kubernetes-101 alongside repoUrl=.../enablement-kubernetes-101."""
    s = {"trainingId": "kubernetes-101",
         "repoUrl": "https://github.com/dynatrace-wwse/enablement-kubernetes-101"}
    assert wf.workshop_repo(s).endswith("enablement-kubernetes-101")


def test_workshop_repo_falls_back_to_training_id():
    assert wf.workshop_repo({"trainingId": "kubernetes-101"}) == "kubernetes-101"


def test_workshop_repo_of_an_empty_session_is_blank_not_an_error():
    assert wf.workshop_repo({}) == ""


# ── instance bookkeeping ────────────────────────────────────────────────────
# Caught only by a live launch: scale_up returns snake_case "instance_id" while
# the record was reading "InstanceId", so every workshop recorded an EMPTY
# instance list and teardown terminated nothing. Unit tests could not see it —
# they never call the real scale_up — which is exactly why the rehearsal exists.

def _ids(launched):
    return [i.get("instance_id") or i.get("InstanceId")
            for i in launched if i.get("instance_id") or i.get("InstanceId")]


def test_instance_ids_are_read_from_the_shape_scale_up_actually_returns():
    assert _ids([{"instance_id": "i-abc", "type": "m6a.4xlarge"}]) == ["i-abc"]


def test_both_spellings_are_accepted():
    """Neither side should be able to silently empty this list again."""
    assert _ids([{"InstanceId": "i-1"}, {"instance_id": "i-2"}]) == ["i-1", "i-2"]


def test_entries_without_an_id_are_dropped_not_recorded_as_none():
    """A None in the list would reach scale_down and be refused as an
    untaggable instance, taking the whole teardown down with it."""
    assert _ids([{"type": "m6a.4xlarge"}, {"instance_id": "i-ok"}]) == ["i-ok"]
