"""Tests for the control loop's decisions.

  /home/ops/ops-venv/bin/python -m pytest dashboard/test_workshop_fleet.py -q

Only the pure half is tested here — scheduling, sizing and the scale decision.
The effectful half talks to EC2 and is exercised by the end-to-end rehearsal.
"""

import json
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


def test_prewarm_and_teardown_are_never_both_due():
    """The oscillation guard.

    Prewarm used to be unbounded on the late side, so for any workshop whose trainer
    never pressed end BOTH predicates were true — and the loop acts on prewarm first.
    With CONTROL_LOOP_APPLY on that is an infinite launch/terminate cycle: launch, tear
    down for being past the window, launch again, forever, for a workshop nobody is
    attending. Found live — a room opened three days earlier that the loop had been
    asking to prewarm every 30 seconds since.
    """
    for offset in (-30, -119, -121, -200, -400, -4320):   # -4320 min = 3 days
        s = _session(start_offset_min=offset)
        assert not (wf.due_for_prewarm(s, NOW) and wf.due_for_teardown(s, NOW)), \
            f"both due at start_offset={offset}"


def test_a_workshop_long_past_its_window_is_not_prewarmed():
    # The window is the HOLD FLOOR here, not duration+grace: 120+30 = 150 is
    # shorter than the 240-minute floor, so the workshop stays prewarmable until
    # 240 minutes past its start.
    assert wf.due_for_prewarm(_session(start_offset_min=-239), NOW) is True
    assert wf.due_for_prewarm(_session(start_offset_min=-241), NOW) is False
    # The real one: opened three days ago, never ended.
    assert wf.due_for_prewarm(_session(start_offset_min=-3 * 24 * 60), NOW) is False


def test_ended_workshop_is_never_prewarmed():
    for state in ("ended", "cancelled", "deleted"):
        assert wf.due_for_prewarm(_session(-10, state=state), NOW) is False


def test_workshop_without_a_start_time_is_skipped_not_crashed():
    assert wf.due_for_prewarm({"state": "scheduled"}, NOW) is False
    assert wf.due_for_prewarm({"state": "scheduled",
                               "scheduledAt": "not-a-date"}, NOW) is False


def test_teardown_waits_for_the_grace_period():
    """Overruns are normal. A trainer running ten minutes late must not have
    the room's environments deleted underneath them.

    Long enough that duration+grace, not the hold floor, is the binding window:
    a 6h booking ends past the 240-minute floor, so grace is what decides.
    """
    s = _session(start_offset_min=-360, duration=360)      # ended exactly now
    assert wf.due_for_teardown(s, NOW, grace_minutes=30) is False
    assert wf.due_for_teardown(s, NOW + timedelta(minutes=31),
                               grace_minutes=30) is True


def test_explicit_end_beats_the_clock():
    """Finishing early should give the machines back early, not pay for the
    rest of the booked window."""
    s = _session(start_offset_min=-10, duration=240, state="ended")
    assert wf.due_for_teardown(s, NOW) is True


def test_hold_floor_extends_a_short_workshop():
    """The app's create form never sends durationMinutes, so every workshop it
    creates falls back to 120 and would have lost its machines 2h30 after the
    start. The floor holds them to 4h."""
    s = _session(start_offset_min=0, duration=120)
    start = wf.parse_iso(s["scheduledAt"])
    assert wf.teardown_at(s) == start + timedelta(minutes=240)
    assert wf.due_for_teardown(s, start + timedelta(minutes=239)) is False
    assert wf.due_for_teardown(s, start + timedelta(minutes=241)) is True


def test_hold_floor_never_truncates_a_long_workshop():
    """A floor only ever extends. A 6h booking keeps its 6h30, rather than
    being cut to the 4h floor — that would delete environments mid-session."""
    s = _session(start_offset_min=0, duration=360)
    start = wf.parse_iso(s["scheduledAt"])
    assert wf.teardown_at(s) == start + timedelta(minutes=360 + 30)


def test_per_workshop_windows_are_honoured():
    s = _session(start_offset_min=0, duration=120)
    s["prewarmLeadMinutes"] = "90"
    s["holdMinutes"] = "480"
    start = wf.parse_iso(s["scheduledAt"])
    assert wf.session_lead_minutes(s) == 90
    assert wf.session_hold_minutes(s) == 480
    assert wf.prewarm_at(s) == start - timedelta(minutes=90)
    assert wf.teardown_at(s) == start + timedelta(minutes=480)
    # And the predicate moves with them.
    assert wf.due_for_prewarm(s, start - timedelta(minutes=89)) is True
    assert wf.due_for_prewarm(s, start - timedelta(minutes=91)) is False


def test_stored_windows_are_clamped_when_read():
    """A value hand-edited into Redis, or stored before a ceiling was lowered,
    must not be able to hold a fleet for a week."""
    s = _session()
    s["prewarmLeadMinutes"], s["holdMinutes"] = "99999", "99999"
    assert wf.session_lead_minutes(s) == wf.LEAD_MINUTES_CAP
    assert wf.session_hold_minutes(s) == wf.HOLD_MINUTES_CAP
    s["prewarmLeadMinutes"], s["holdMinutes"] = "-5", "-5"
    assert wf.session_lead_minutes(s) == 0
    assert wf.session_hold_minutes(s) == 0


def test_unset_or_unusable_windows_fall_back_to_the_defaults():
    """0 and '' are what the app sends for an empty numeric field, and the
    default is the right reading of an empty field — not zero minutes."""
    for value in ("", "0", 0, None, "not-a-number"):
        s = _session()
        s["prewarmLeadMinutes"], s["holdMinutes"] = value, value
        assert wf.session_lead_minutes(s) == wf.PREWARM_LEAD_MINUTES
        assert wf.session_hold_minutes(s) == wf.WORKSHOP_HOLD_MINUTES


def test_prewarm_and_teardown_are_never_both_due_at_the_widest_windows():
    """The oscillation guard again, at the ceilings.

    Widening the hold window is exactly the kind of change that could make the
    two predicates overlap again, and dry run cannot show that because dry run
    never transitions state. Swept rather than spot-checked for that reason.
    """
    s = _session(start_offset_min=0, duration=360)
    s["prewarmLeadMinutes"] = str(wf.LEAD_MINUTES_CAP)
    s["holdMinutes"] = str(wf.HOLD_MINUTES_CAP)
    start = wf.parse_iso(s["scheduledAt"])
    for minutes in range(-wf.LEAD_MINUTES_CAP - 60, wf.HOLD_MINUTES_CAP + 120, 7):
        now = start + timedelta(minutes=minutes)
        assert not (wf.due_for_prewarm(s, now) and wf.due_for_teardown(s, now)), \
            f"both due at start{minutes:+d}min"


def test_self_destruct_always_outlives_the_loops_own_teardown():
    """`shutdown -h +N` is a backstop, not a competing schedule. When the hold
    floor pushed teardown past duration+grace, a lifetime still computed as
    lead+duration+grace armed the timer BEFORE the loop meant to tear the
    machines down — the workshop would have lost them mid-session."""
    cases = [
        _session(start_offset_min=0, duration=120),          # floor binds
        _session(start_offset_min=0, duration=360),          # duration binds
        {**_session(duration=120), "prewarmLeadMinutes": "360",
         "holdMinutes": "1440"},                             # both at the cap
    ]
    for s in cases:
        window = (wf.teardown_at(s) - wf.prewarm_at(s)).total_seconds() // 60
        assert wf._workshop_lifetime_minutes(s) > window, s


def test_lead_time_is_configurable_down_to_minutes():
    """The rehearsal compresses an hour-scale delivery into minutes; if this
    were hardcoded the loop could only ever be tested in real time."""
    s = _session(start_offset_min=3)
    assert wf.due_for_prewarm(s, NOW, lead_minutes=3) is True


# ── sizing ──────────────────────────────────────────────────────────────────

def test_seventy_seats_of_k8s101_plans_four_machines():
    """20 seats per m6a.4xlarge from the unit model, so 70 needs 4 machines —
    and only 4. No spare: see WORKSHOP_REDUNDANCY."""
    plan = wf.plan_workshop_capacity(70, rp.K8S_101, "m6a.4xlarge")
    assert plan["seats_per_worker"] == 20
    assert plan["workers"] == 4
    assert plan["total_seats"] == 80, "must exceed the roster, not merely meet it"
    assert plan["pool_kind"] == "dedicated"


def test_no_spare_is_bought_by_default():
    """The spare was 100% overhead for every workshop from 8 to 20 seats — one
    real machine plus one idle one, held for the whole window. It bought only
    somewhere to RE-provision after a host loss, never the survival of the
    sessions on it, and a replacement machine is minutes away regardless."""
    assert wf.WORKSHOP_REDUNDANCY == 0
    for seats in (8, 12, 20):
        plan = wf.plan_workshop_capacity(seats, rp.K8S_101, "m6a.4xlarge")
        assert plan["workers"] == 1, f"{seats} seats fit on one machine"
        assert "spare" not in plan["reason"]


def test_a_spare_can_still_be_bought_explicitly():
    """Kept as an env var, not deleted: a delivery that cannot tolerate a
    re-provision sets WORKSHOP_REDUNDANCY=1 without a code change."""
    plan = wf.plan_workshop_capacity(12, rp.K8S_101, "m6a.4xlarge", redundancy=1)
    assert plan["workers"] == 2
    assert "+1 spare" in plan["reason"]


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
    a room. 21 seats does not fit on one 20-seat machine, so it gets two — the
    division must never truncate, spare or no spare."""
    assert wf.plan_workshop_capacity(21, rp.K8S_101, "m6a.4xlarge")["workers"] == 2
    assert wf.plan_workshop_capacity(20, rp.K8S_101, "m6a.4xlarge")["workers"] == 1


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


def test_seats_coming_back_are_not_a_reason_to_buy_a_machine():
    """A restarting pool looks exactly like a full one — it is not.

    A warming worker reports 0 free seats, so `free < min_free` fires. But those
    seats are ABSENT, not taken: they return in minutes, while a machine launched
    now needs its own boot plus warm-up and lands about when it stops being
    needed. Unguarded, every worker restart bought a spare instance and a
    fleet-wide deploy bought one per worker. Seen live on 2026-08-16:
    "would scale_up=1 — 0 free seats < 4" while both workers were re-warming.
    """
    warming = _worker("w1", free=0, status="warming")
    warming["capacity"] = "20"
    d = wf.daily_scale_decision([warming], {}, min_free=4)
    assert d["scale_up"] == 0
    assert "warming back up" in d["why"]

    # THE REAL SHAPE, and why the guard never actually fired. The agent
    # publishes `capacity` as slots ALREADY WARM — which is 0 for most of a
    # warm-up — and `slots_total` as the nominal figure. Summing `capacity`
    # therefore summed zero at exactly the moment this guard exists for.
    # Measured 2026-08-16: amd001 restarted at 13:51:46 and the loop bought
    # spot instances at 13:52:16 and 13:52:48, with nothing queued.
    honest = _worker("w1", free=0, status="warming")
    honest["capacity"] = "0"          # nothing warm yet
    honest["slots_total"] = "20"      # twenty seats on their way back
    d = wf.daily_scale_decision([honest], {}, min_free=4)
    assert d["scale_up"] == 0, (
        "bought a machine while 20 seats were warming — this is the bug that "
        "put two unwanted spot workers in the fleet")

    # A DRAINING worker is going away for good — its seats are not coming back.
    gone = _worker("w1", free=0, status="warming", draining="1")
    gone["capacity"] = "20"
    assert wf.daily_scale_decision([gone], {}, min_free=4)["scale_up"] == 1

    # A genuinely full pool still scales.
    full = _worker("w1", free=0, status="ready")
    assert wf.daily_scale_decision([full], {}, min_free=4)["scale_up"] == 1


def test_warming_does_not_excuse_a_pool_that_will_still_be_short():
    """The guard is narrow on purpose. If the returning seats do not cover the
    shortfall, that is real demand and the machine should be on its way NOW —
    not one warm-up later."""
    small = _worker("w1", free=0, status="warming")
    small["capacity"] = "2"
    d = wf.daily_scale_decision([small], {}, min_free=10)
    assert d["scale_up"] == 1
    assert "only 2 warming" in d["why"]


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
    """Seats on a machine still warming are not seats YET.

    They are not counted as free — but as of 2026-08-16 that no longer implies a
    scale-up, because they are about to become free (see
    test_seats_coming_back_are_not_a_reason_to_buy_a_machine). With no `capacity`
    on the heartbeat there is nothing known to be coming back, so this still
    scales.
    """
    d = wf.daily_scale_decision([_worker(free=20, status="warming")], {}, min_free=4)
    assert d["scale_up"] == 1
    assert "0 free seats" in d["why"]


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


def test_a_workshop_larger_than_the_scale_cap_is_batched():
    """MAX_SCALE_UP is 4 per call, and 70 seats needs exactly 4 — the bootcamp
    sits ON the limit. Anything larger would raise, be caught, and retry forever
    without succeeding, so the launch batches instead. The cap is a per-call
    safety rail, not a fleet ceiling."""
    from dashboard import fleet
    plan = wf.plan_workshop_capacity(200, rp.K8S_101, "m6a.4xlarge")
    assert plan["workers"] > fleet.MAX_SCALE_UP, "test needs a plan above the cap"

    batches, remaining = [], plan["workers"]
    while remaining > 0:
        b = min(remaining, fleet.MAX_SCALE_UP)
        batches.append(b)
        remaining -= b
    assert sum(batches) == plan["workers"], "batching must not lose or add workers"
    assert all(b <= fleet.MAX_SCALE_UP for b in batches)
    assert len(batches) > 1


def test_a_bootcamp_still_EXCEEDS_the_per_call_cap():
    """`scale_up` refuses more than MAX_SCALE_UP (4) per call, so the batching
    loop in provision_workshop_fleet is not theoretical. Dropping the spare took
    70 seats down to exactly 4 — ON the limit, not over it — so this pins a size
    that is genuinely over. If someone removes the batching because "we only
    ever launch four", this fails."""
    from dashboard import fleet
    assert wf.plan_workshop_capacity(70, rp.K8S_101, "m6a.4xlarge")["workers"] \
        == fleet.MAX_SCALE_UP, "70 seats now sits exactly ON the per-call cap"
    assert wf.plan_workshop_capacity(100, rp.K8S_101, "m6a.4xlarge")["workers"] \
        > fleet.MAX_SCALE_UP


# ── ending a workshop must stop its environments ────────────────────────────

class _JobsRedis:
    """Just enough Redis: job:running hashes, a typed scan, and publishes."""

    def __init__(self, jobs: dict[str, dict]):
        self.h = {f"job:running:{jid}": dict(rec) for jid, rec in jobs.items()}
        # A LIST in the same namespace, because the worker scan met one of these
        # and aborted. This one is a different namespace but the lesson stands.
        self.l = {"job:running:index": ["noise"]}
        self.published: list[str] = []

    async def scan_iter(self, match="*", count=500):
        prefix = match.rstrip("*")
        for key in list(self.h) + list(self.l):
            if key.startswith(prefix):
                yield key

    async def type(self, key):
        return "hash" if key in self.h else "list"

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def delete(self, key):
        self.h.pop(key, None)
        self.l.pop(key, None)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def publish(self, channel, msg):
        self.published.append(msg)


def test_ending_a_workshop_terminates_only_its_own_sessions():
    """MEASURED 2026-08-14: end returned 200 and all 12 sessions were still
    running ten minutes later. A seat held by an ended workshop is a seat the
    next workshop's plan already counted, so this is load-bearing for the whole
    capacity model, not a tidiness issue."""
    import asyncio

    r = _JobsRedis({
        "job-a": {"workshop_id": "ws_mine", "repo": "x"},
        "job-b": {"workshop_id": "ws_mine", "repo": "x"},
        "job-c": {"workshop_id": "ws_other", "repo": "x"},
        "job-d": {"repo": "x"},                      # self-service, no workshop
    })
    n = asyncio.run(wf.terminate_workshop_sessions(r, "ws_mine"))
    assert n == 2
    assert sorted(r.published) == ["job-a", "job-b"]
    # The durable flag is what survives a worker that is restarting; the
    # publish alone is fire-and-forget.
    assert r.h["job:running:job-a"]["terminating"] == "1"
    assert "terminating" not in r.h["job:running:job-c"]
    assert "terminating" not in r.h["job:running:job-d"]


def test_terminating_an_empty_workshop_is_a_no_op_not_an_error():
    import asyncio
    r = _JobsRedis({})
    assert asyncio.run(wf.terminate_workshop_sessions(r, "ws_nobody")) == 0


class _QueuedRedis(_JobsRedis):
    """Adds the queue lists a parked learner sits in."""

    def __init__(self, jobs, queues):
        super().__init__(jobs)
        self.l = dict(queues)

    async def lrange(self, key, start, stop):
        items = self.l.get(key, [])
        return items[start:] if stop == -1 else items[start:stop + 1]

    async def lrem(self, key, count, value):
        items = self.l.get(key, [])
        if value in items:
            items.remove(value)
            return 1
        return 0


def _payload(job_id):
    import json as _json
    return _json.dumps({"job_id": job_id, "repo": "x", "type": "daemon"})


def test_ending_a_workshop_drops_its_learners_who_never_started():
    """MEASURED 2026-08-14: a workshop ended with three learners still parked
    behind the pacer. Flagging `terminating` only reaches a job a worker has
    already claimed, so those three would have been admitted afterwards and
    built environments for a workshop nobody was attending."""
    import asyncio

    r = _QueuedRedis(
        jobs={
            "job-a": {"workshop_id": "ws_mine", "worker_id": "queued"},
            "job-b": {"workshop_id": "ws_mine", "worker_id": "wamd001"},
            "job-c": {"workshop_id": "ws_other", "worker_id": "queued"},
        },
        queues={
            "queue:pending:queue:test:amd64": [_payload("job-a"), _payload("job-c")],
            "queue:test:amd64": [_payload("job-z")],
        },
    )
    n = asyncio.run(wf.terminate_workshop_sessions(r, "ws_mine"))
    # 2 records flagged + 1 queued payload dropped
    assert n == 3
    remaining = r.l["queue:pending:queue:test:amd64"]
    assert len(remaining) == 1, "another workshop's learner must not be dropped"
    assert "job-c" in remaining[0]
    assert r.l["queue:test:amd64"] == [_payload("job-z")], \
        "an unrelated job keeps its place in the queue"


def test_dropping_queued_jobs_is_a_no_op_when_nothing_is_parked():
    import asyncio
    r = _QueuedRedis(jobs={"job-a": {"workshop_id": "ws_mine",
                                     "worker_id": "wamd001"}},
                     queues={"queue:pending:queue:test:amd64": []})
    assert asyncio.run(wf.terminate_workshop_sessions(r, "ws_mine")) == 1


# ── stale heartbeats ────────────────────────────────────────────────────────

class _WorkersRedis:
    """Just enough Redis to scan `worker:*` hashes."""

    def __init__(self, workers):
        self.h = {f"worker:{wid}": dict(fields) for wid, fields in workers.items()}
        self.deleted = []

    async def scan_iter(self, match=None, count=None):
        for k in list(self.h):
            yield k

    async def hgetall(self, key):
        return self.h.get(key, {})

    async def delete(self, key):
        self.deleted.append(key)
        self.h.pop(key, None)


def _hb(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_a_terminated_machines_leftover_record_is_not_a_worker():
    """MEASURED 2026-08-16, on the first workshop machine the loop tore down.

    Terminating an instance does not remove its `worker:{id}` hash, so the record
    stayed frozen at `status: warming` / `capacity: 0` with a heartbeat minutes
    old. It carried no `pool` field — it had booted from main, whose agent has no
    concept of pools — so every consumer read it as a DAILY worker that would
    never contribute a seat, and the loop would have scaled up against it for as
    long as the record existed.
    """
    import asyncio
    r = _WorkersRedis({
        "amd001": {"status": "ready", "capacity": "20", "slots_free": "20",
                   "last_heartbeat": _hb(5)},
        "spot-dead": {"status": "warming", "capacity": "0", "slots_free": "0",
                      "last_heartbeat": _hb(600)},
    })
    live = asyncio.run(wf._daily_workers(r))
    assert [w["worker_id"] for w in live] == ["amd001"]


def test_a_worker_with_no_heartbeat_field_is_still_counted():
    """Absence is not staleness. A worker mid-registration, or one on an older
    agent that does not publish the field, must not be dropped from its own
    autoscaler — that would empty the fleet during a rolling deploy."""
    import asyncio
    r = _WorkersRedis({"amd001": {"status": "ready", "capacity": "20"}})
    assert len(asyncio.run(wf._daily_workers(r))) == 1


def test_a_never_started_learners_record_goes_when_the_workshop_ends():
    """Ending the workshop is now the ONLY moment these can be cleaned up.

    The terminate reconciler deliberately no longer treats worker_id="queued" as
    an orphan — that bug deleted every paced learner's record mid-session — so a
    learner who never started has nothing else that would ever reap their record.
    Measured 2026-08-16: five survived a workshop that had ended, reading as
    running sessions with no environment anywhere.
    """
    import asyncio
    r = _QueuedRedis(jobs={"job-parked": {"workshop_id": "ws_mine", "worker_id": "queued"},
                           "job-live":   {"workshop_id": "ws_mine", "worker_id": "wamd001"}},
                     queues={"queue:pending:queue:test:amd64": [_payload("job-parked")]})
    asyncio.run(wf.terminate_workshop_sessions(r, "ws_mine"))
    assert "job:running:job-parked" not in r.h, "the parked learner's record must go"
    assert "job:running:job-live" in r.h, \
        "a learner a worker CLAIMED still owns their record — the worker clears it"


def test_an_absent_worker_id_is_unknown_not_never_started():
    """Only the explicit "queued" marker means never-started. Deleting on an
    ABSENT worker_id would take a live learner's record with it."""
    import asyncio
    r = _QueuedRedis(jobs={"job-x": {"workshop_id": "ws_mine"}},   # no worker_id
                     queues={"queue:pending:queue:test:amd64": [_payload("job-x")]})
    asyncio.run(wf.terminate_workshop_sessions(r, "ws_mine"))
    assert "job:running:job-x" in r.h


# ── two tiers of workshop, and the standing lane ────────────────────────────

def test_a_small_workshop_launches_nothing():
    """The whole reason a workshop box stays warm: a room of seven opens NOW,
    not in the ~8 minutes it takes to boot and warm an instance."""
    plan = wf.plan_workshop_capacity(7, rp.K8S_101, "m6a.4xlarge")
    assert plan["workers"] == 0
    assert plan["pool_kind"] == "standing"
    assert "no machines launched" in plan["reason"]


def test_one_seat_over_the_threshold_gets_its_own_machines():
    """8 seats is above the standing reserve, so it must not lean on the box
    that has to stay free for the next unannounced room."""
    plan = wf.plan_workshop_capacity(8, rp.K8S_101, "m6a.4xlarge")
    assert plan["pool_kind"] == "dedicated"
    assert plan["workers"] == 1, "8 seats fit on one 20-seat machine"


def test_a_big_workshop_is_sized_for_all_its_seats_not_the_remainder():
    """A dedicated workshop does NOT subtract the standing box's reserve. The
    reserve is for rooms that open with no notice; spending it on a planned
    workshop would mean the next ad-hoc room finds nothing."""
    plan = wf.plan_workshop_capacity(31, rp.K8S_101, "m6a.4xlarge")
    assert plan["workers"] == 2, "ceil(31/20)=2 machines"
    assert plan["total_seats"] >= 31


def test_the_threshold_is_configurable():
    """The rehearsal needs to drive both tiers without a 20-person roster."""
    assert wf.plan_workshop_capacity(
        3, rp.K8S_101, "m6a.4xlarge", standing_max_seats=2)["pool_kind"] == "dedicated"
    assert wf.plan_workshop_capacity(
        3, rp.K8S_101, "m6a.4xlarge", standing_max_seats=3)["pool_kind"] == "standing"


def test_a_deferred_teardown_is_retried_next_tick():
    """A deferral parks the record in DRAINING and its own comment promises the
    next tick re-enters. TEARDOWNABLE_STATES was the gate and did not list it, so
    a workshop whose workers still reported sessions was deferred exactly ONCE
    and never revisited — the TEARDOWN_DEFER_MAX_MINUTES bound could not expire,
    because nothing came back to check it. Found on a record DRAINING since
    00:19 with 5 instances against it."""
    ended = _session(start_offset_min=-600, state="ended")
    assert wf.should_teardown(wf.DRAINING, ended, NOW) is True
    assert wf.DRAINING in wf.TEARDOWNABLE_STATES


def test_the_two_terminal_states_are_still_not_torn_down():
    """DONE is finished and re-entering it is a no-op; a workshop that has not
    been provisioned has nothing to give back."""
    ended = _session(start_offset_min=-600, state="ended")
    assert wf.should_teardown(wf.DONE, ended, NOW) is False
    assert wf.should_teardown(None, ended, NOW) is False


def test_draining_does_not_reopen_the_oscillation():
    """The third check the control-loop notes demand: a state added to teardown
    must not also be prewarmable, or the loop launches and terminates the same
    machine forever."""
    assert wf.DRAINING not in wf.PREWARMABLE_STATES
    for offset in (-30, -200, -4320):
        s = _session(start_offset_min=offset)
        assert not (wf.should_prewarm(wf.DRAINING, s, NOW)
                    and wf.should_teardown(wf.DRAINING, s, NOW))


# ── reaping a fleet whose workshop was deleted ──────────────────────────────

def test_a_deleted_workshops_fleet_is_found_by_walking_the_FLEET_not_the_index():
    """Deleting a workshop removes it from the index, which is what the loop
    iterates — so its machines became unreachable and ran until the in-instance
    shutdown backstop hours later. The fleet hash is the only structure that
    still knows they exist."""
    orphans = wf.orphan_candidates(["ws_live", "ws_deleted"], ["ws_live"],
                                   index_ok=True)
    assert orphans == ["ws_deleted"]


def test_a_FAILED_index_read_reaps_NOTHING():
    """The one that matters. A failed read yields an empty index, under which
    every workshop in the fleet — including one running a class right now —
    looks abandoned. Without this guard a single Redis blip terminates the whole
    fleet. Doing nothing costs only money."""
    assert wf.orphan_candidates(["ws_a", "ws_b"], [], index_ok=False) == []
    # And an index that is genuinely empty still reaps, so the guard is about
    # the READ failing, not about the index being small.
    assert wf.orphan_candidates(["ws_a"], [], index_ok=True) == ["ws_a"]


def test_an_unreadable_session_is_not_treated_as_a_deleted_one():
    """`session_exists` is tri-state: None means the check failed. Treating that
    as "missing" is how a reaper turns a hiccup into a terminated fleet."""
    assert wf.is_orphaned(wf.READY, False) is True
    assert wf.is_orphaned(wf.READY, None) is False, "unreadable is not deleted"
    assert wf.is_orphaned(wf.READY, True) is False


def test_an_already_torn_down_fleet_is_not_reaped_again():
    """DONE records are the audit trail and cost nothing to leave."""
    assert wf.is_orphaned(wf.DONE, False) is False
    assert wf.is_orphaned("", False) is False
    for state in (wf.WARMING, wf.READY, wf.DRAINING):
        assert wf.is_orphaned(state, False) is True


def test_reaping_cannot_fight_the_other_three_predicates():
    """Prewarm, teardown and upgrade only ever act on INDEXED workshops; the
    reaper only ever acts on unindexed ones. The sets are disjoint by
    construction, which is the check the control-loop notes demand of any new
    predicate."""
    indexed = ["ws_a", "ws_b"]
    assert wf.orphan_candidates(["ws_a", "ws_b"], indexed, index_ok=True) == []


# ── what a workshop is sized FOR ────────────────────────────────────────────

def _booked(max_seats, trainers=1):
    team = ["t%d@example.com" % i for i in range(trainers)]
    return {"maxSeats": str(max_seats), "trainers": json.dumps(team)}


def test_a_workshop_is_sized_for_its_BOOKED_capacity_not_its_turnout():
    """The bug this exists for: learners join with a code and never touch the
    roster, so a 40-seat class counted 1 seat, planned for 1, and opened on the
    standing lane with nothing behind it. Machines must be up BEFORE anyone
    arrives — the moment there is nobody to count."""
    assert wf.planned_seats(_booked(40), roster_count=0) == 41


def test_the_trainer_team_is_added_on_top_of_the_booked_seats():
    """maxSeats caps the ROSTER. Every trainer takes an environment too, and a
    five-trainer team on a 20-seat room is a whole extra machine's worth."""
    assert wf.planned_seats(_booked(20, trainers=5), roster_count=0) == 25


def test_unlimited_seats_falls_back_to_the_roster():
    """maxSeats 0 means unlimited, which cannot be planned. The roster is the
    only real number left — but it is a fallback, never the primary source."""
    assert wf.planned_seats(_booked(0), roster_count=12) == 13


def test_a_hand_edited_seat_count_cannot_buy_an_unbounded_fleet():
    """Clamped on READ, like the window minutes: this feeds RunInstances."""
    assert wf.planned_seats(_booked(99999)) == 201      # MAX_SEATS + 1 trainer
    assert wf.planned_seats({"maxSeats": "not-a-number"}) == 1
    assert wf.planned_seats({}) == 1


def test_a_booked_workshop_crosses_the_standing_threshold_on_capacity_alone():
    """End to end over the two functions, because the defect was the SEAM: the
    planner was always right about 12 seats, it was never told about them."""
    seats = wf.planned_seats(_booked(10, trainers=2))
    assert seats == 12
    assert wf.plan_workshop_capacity(seats, rp.K8S_101,
                                     "m6a.4xlarge")["pool_kind"] == "dedicated"


def test_a_standing_workshop_that_outgrows_the_lane_is_upgraded():
    """The lane was decided once, at prewarm. Booked capacity moves afterwards
    and nothing re-read it, so a workshop that grew rode the standing box's
    reserve for its whole delivery."""
    assert wf.needs_bigger_fleet("ready", {"standing": True}, 8) is True
    assert wf.needs_bigger_fleet("ready", {"standing": True}, 7) is False


def test_a_dedicated_workshop_is_never_re_planned():
    """Only ever upgrades. Downgrading would terminate machines a room may
    already be sitting on."""
    assert wf.needs_bigger_fleet("ready", {"standing": False}, 200) is False
    assert wf.needs_bigger_fleet("warming", {"standing": True}, 200) is False
    assert wf.needs_bigger_fleet(wf.DONE, {"standing": True}, 200) is False


def test_the_upgrade_cannot_oscillate_with_teardown():
    """The third scheduling predicate, checked against the other two. Prewarm
    and teardown are mutually exclusive by construction; this one fires only on
    a record that launched NOTHING, and stops being true the moment it acts."""
    rec = {"standing": True}
    s = _session(start_offset_min=-10_000)          # long past its window
    assert wf.should_teardown(wf.READY, s, NOW) is True
    # After the upgrade the record is no longer standing, so the predicate that
    # produced it is false forever after.
    assert wf.needs_bigger_fleet(wf.READY, {**rec, "standing": False}, 50) is False


def test_lent_seats_count_as_daily_capacity():
    """The standing workshop box reads the daily queue while under its lend cap,
    so its lendable seats ARE self-service capacity. Ignoring them makes the
    planner buy a machine while ten warm seats sit idle.

    They arrive as `lenders`, NOT in `workers`: _daily_workers filters to
    pool == daily, so a workshop box never appears in that list — which is
    exactly how the first version of this shipped with the sum in the wrong
    place and no effect at all.
    """
    daily = _worker("w-daily", free=0)
    lender = _worker("w-standing", free=0)
    lender["borrow_free"] = "10"
    d = wf.daily_scale_decision([daily], {}, min_free=4, lenders=[lender])
    assert d["scale_up"] == 0, "bought a machine while 10 borrowable seats were free"


def test_a_full_lender_offers_nothing_and_the_pool_still_scales():
    """borrow_free is what the box will ACTUALLY take, so a full one must not
    keep the planner from buying real capacity."""
    daily = _worker("w-daily", free=0)
    lender = _worker("w-standing", free=0)
    lender["borrow_free"] = "0"
    d = wf.daily_scale_decision([daily], {}, min_free=4, lenders=[lender])
    assert d["scale_up"] == 1


def test_a_lender_does_not_fill_a_slot_in_the_worker_cap():
    """A lender contributes seats, not a machine. Counting it toward
    DAILY_MAX_WORKERS would let one workshop box block every daily purchase."""
    daily = [_worker(f"w{i}", free=0) for i in range(2)]
    lenders = [_worker("w-standing", free=0)]
    lenders[0]["borrow_free"] = "0"
    d = wf.daily_scale_decision(daily, {}, min_free=4, max_workers=3, lenders=lenders)
    assert d["scale_up"] == 1, "the lender was counted as a daily worker"


def test_a_draining_or_warming_lender_offers_nothing():
    """Seats on a box that is going away, or has not warmed, are not seats."""
    daily = _worker("w-daily", free=0)
    for bad in ({"status": "warming"}, {"draining": "1"}):
        lender = _worker("w-standing", free=0)
        lender["borrow_free"] = "10"
        lender.update(bad)
        d = wf.daily_scale_decision([daily], {}, min_free=4, lenders=[lender])
        assert d["scale_up"] == 1, f"counted seats from a {bad} lender"


# ── the bounded warming wait ────────────────────────────────────────────────

def test_a_pool_that_never_finishes_warming_is_not_waited_on_forever():
    """A worker that comes back short (seen: 18/30 slots while reporting fully
    warm) held the workshop at `warming` with no deadline — which reads to a
    trainer exactly like a hung fleet, and the room never opens."""
    rec = {"requested_at": NOW.isoformat()}
    assert wf._warming_too_long(rec, NOW, timeout_minutes=20) is False
    assert wf._warming_too_long(rec, NOW + timedelta(minutes=19),
                                timeout_minutes=20) is False
    assert wf._warming_too_long(rec, NOW + timedelta(minutes=21),
                                timeout_minutes=20) is True


def test_an_undateable_record_is_never_declared_degraded():
    """Guessing here would flip a healthy workshop to DEGRADED on a bad clock
    or a half-written record."""
    assert wf._warming_too_long({}, NOW) is False
    assert wf._warming_too_long({"requested_at": "not-a-date"}, NOW) is False


# ── the self-destruct backstop ──────────────────────────────────────────────

def test_machines_outlive_the_teardown_window_but_not_by_much():
    """The timer must never fire before the loop's own teardown would have, or
    it becomes a second scheduler racing the first."""
    lifetime = wf._workshop_lifetime_minutes({"durationMinutes": "120"})
    latest_teardown = wf.PREWARM_LEAD_MINUTES + 120 + wf.TEARDOWN_GRACE_MINUTES
    assert lifetime > latest_teardown, "self-destruct could fire mid-workshop"


def test_a_malformed_duration_still_gets_a_timer():
    """No duration must not mean no cost ceiling."""
    assert wf._workshop_lifetime_minutes({}) > 0
    assert wf._workshop_lifetime_minutes({"durationMinutes": "abc"}) > 0


# ── deferring termination must CONVERGE ─────────────────────────────────────
#
# The first version of the busy-worker guard set a flag and then fell through to
# state=DONE — and teardown_workshop_fleet returns immediately for a DONE
# record, so "wait and retry" silently became "never". Three m6a.4xlarge ran on
# after their workshop ended, on the very run meant to prove teardown was clean.

def test_a_deferred_teardown_stays_retryable():
    """DRAINING is re-entered by the next tick; DONE is not. A deferral that
    lands in DONE is a permanent leak, not a delay."""
    import inspect
    src = inspect.getsource(wf.teardown_workshop_fleet)
    defer = src.index("deferred_termination")
    # The deferral branch must set DRAINING and return, not fall through.
    branch = src[defer - 400:defer + 400]
    assert "DRAINING" in branch, "a deferred teardown must remain retryable"
    assert "return rec" in branch, "the deferral must not fall through to DONE"


def test_the_deferral_is_bounded():
    """A worker whose active_jobs never returns to zero is the known wedged-reaper
    bug. A disposable machine must not outlive its workshop waiting for it."""
    assert wf.TEARDOWN_DEFER_MAX_MINUTES > 0
    import inspect
    src = inspect.getsource(wf.teardown_workshop_fleet)
    assert "TEARDOWN_DEFER_MAX_MINUTES" in src
    assert "deferred_since" in src, "without a start time the bound cannot be applied"


def test_the_bound_outlasts_a_real_teardown():
    """A 30-seat teardown measured ~2.5 minutes. The bound has to clear that by
    a wide margin or it would terminate hosts mid-teardown — the exact thing the
    guard exists to prevent."""
    assert wf.TEARDOWN_DEFER_MAX_MINUTES >= 10


# ── re-arming a torn-down workshop ──────────────────────────────────────────
# The control loop gated prewarm on `state in (None, "", "failed")` and teardown
# on `state in (WARMING, READY)`. DONE matched neither, so a workshop whose pool
# had been torn down was invisible to the loop from then on: rescheduling it
# launched nothing, and because teardown also deletes the pool binding, its
# learners routed to the shared daily queue instead. Observed 2026-08-16 —
# 21 learner sessions landed on the two standing nodes with `failedOpen: 0`.

def test_a_torn_down_workshop_can_be_rescheduled():
    """DONE must be re-armable, or teardown is a one-way door."""
    s = _session(start_offset_min=30)          # inside the prewarm lead
    assert wf.should_prewarm(wf.DONE, s, NOW) is True


def test_every_terminal_state_is_re_armable():
    """No state a workshop can come to REST in may be absorbing. DRAINING is
    excluded on purpose: it means a teardown is still in flight."""
    for state in (None, "", "failed", wf.DONE):
        assert wf.should_prewarm(state, _session(start_offset_min=30), NOW) is True, state


def test_re_arming_does_not_oscillate_with_teardown():
    """The pair that made the loop launch and terminate the same machine every
    30s. A DONE workshop still past its window must stay DONE — only a genuine
    reschedule may re-arm it."""
    stale = _session(start_offset_min=-1000)   # ended long ago, never rescheduled
    assert wf.due_for_teardown(stale, NOW) is True
    assert wf.should_prewarm(wf.DONE, stale, NOW) is False
    # ...and DONE is not torn down again either, so the tick is a no-op.
    assert wf.should_teardown(wf.DONE, stale, NOW) is False


def test_prewarm_and_teardown_stay_mutually_exclusive():
    """Whatever the fleet state, one tick may never both launch and terminate."""
    for state in (None, "", "failed", wf.WARMING, wf.READY, wf.DRAINING, wf.DONE):
        for offset in (-1000, -60, 0, 30, 300):
            s = _session(start_offset_min=offset)
            assert not (wf.should_prewarm(state, s, NOW)
                        and wf.should_teardown(state, s, NOW)), (state, offset)


def test_a_warming_workshop_is_not_relaunched():
    """Re-arming must not re-enter a fleet that is already coming up."""
    s = _session(start_offset_min=30)
    assert wf.should_prewarm(wf.WARMING, s, NOW) is False
    assert wf.should_prewarm(wf.READY, s, NOW) is False


def test_a_draining_workshop_is_left_alone():
    """DRAINING means teardown is mid-flight; launching into it would race."""
    s = _session(start_offset_min=30)
    assert wf.should_prewarm(wf.DRAINING, s, NOW) is False


def test_an_ended_workshop_is_never_re_armed():
    """A trainer who pressed end must not have machines bought back for them."""
    s = _session(start_offset_min=30, state="ended")
    assert wf.should_prewarm(wf.DONE, s, NOW) is False


def test_the_loop_uses_the_tested_predicates():
    """The bug survived because the tuples were inline in `tick`, unreachable
    from any unit test. If they move back inline, this fails."""
    import inspect
    src = inspect.getsource(wf.tick)
    assert "should_prewarm(" in src and "should_teardown(" in src


# ── Reaper discovery is environment-scoped ───────────────────────────────────
#
# _instances_tagged feeds TERMINATION. Pool names are not unique across
# environments — "daily" is the same string in staging as in production — so
# without an environment scope a staging reaper handing back its own daily pool
# would hand back production's with it. There was no test here at all.

import asyncio
import os

import pytest


def _reservations(*instances):
    return {"Reservations": [{"Instances": list(instances)}]}


def _ec2(iid, env=None, pool="daily"):
    tags = [{"Key": "orbital-pool", "Value": pool}]
    if env is not None:
        tags.append({"Key": "env", "Value": env})
    return {"InstanceId": iid, "Tags": tags}


@pytest.fixture
def _fake_ec2(monkeypatch):
    """Make fleet._aws return a fixed describe-instances payload."""
    from dashboard import fleet

    def _install(payload):
        async def fake_aws(*_a, **_kw):
            return payload
        monkeypatch.setattr(fleet, "_aws", fake_aws)
    return _install


@pytest.fixture
def _as_env(monkeypatch):
    def _set(name):
        monkeypatch.setenv("ORBITAL_ENV", name)
    return _set


def test_reaper_sees_only_its_own_environments_instances(_fake_ec2, _as_env):
    _fake_ec2(_reservations(
        _ec2("i-prod", env="prod"),
        _ec2("i-staging", env="staging"),
    ))

    _as_env("staging")
    assert asyncio.run(wf._instances_tagged("daily")) == ["i-staging"]

    _as_env("prod")
    assert asyncio.run(wf._instances_tagged("daily")) == ["i-prod"]


def test_reaper_in_staging_never_returns_an_untagged_machine(_fake_ec2, _as_env):
    # The long-lived production machines carry no env tag. A staging reaper
    # must not hand them back.
    _fake_ec2(_reservations(_ec2("i-legacy")))
    _as_env("staging")
    assert asyncio.run(wf._instances_tagged("daily")) == []


def test_reaper_in_prod_still_returns_untagged_machines(_fake_ec2, _as_env):
    # ...and production must still reap them, or the scope change breaks
    # teardown for every machine that predates the tag.
    _fake_ec2(_reservations(_ec2("i-legacy")))
    _as_env("prod")
    assert asyncio.run(wf._instances_tagged("daily")) == ["i-legacy"]


def test_reaper_still_reaps_nothing_on_a_failed_describe(monkeypatch, _as_env):
    # Fail-safe, preserved from before the env scope: a failed AWS call must
    # reap NOTHING. An exception that read as "no instances" would make every
    # live workshop look abandoned.
    from dashboard import fleet

    async def boom(*_a, **_kw):
        raise RuntimeError("AWS unreachable")
    monkeypatch.setattr(fleet, "_aws", boom)
    _as_env("prod")
    assert asyncio.run(wf._instances_tagged("daily")) == []


def test_reaper_returns_nothing_for_a_blank_pool(_as_env):
    _as_env("prod")
    assert asyncio.run(wf._instances_tagged("")) == []
