"""Tests for the fleet autoscaling policy (phase 3).

Pure logic — no AWS, no Redis — so it runs anywhere.

  - pytest:     python3 -m pytest dashboard/test_fleet_policy.py
  - standalone: cd ops-server && python3 -m dashboard.test_fleet_policy

The load-bearing contracts, each traceable to something that actually went
wrong or was actually measured on 2026-08-12:

* The capacity model must reproduce reality: 6 sessions on a c5.2xlarge, which
  is what production runs and what survived the load test (12 did not).
* free_effective must include IN-FLIGHT launches, or a 4-minute boot against a
  15-second tick launches the same capacity over and over.
* A worker that is stale, cordoned or still warming must not be counted as
  usable capacity.
* Un-draining is always preferred to launching: instant and free vs minutes
  and billed.
* Scale-down only ever cordons. Sessions are immovable, so an instance can only
  be terminated once it is empty.
"""

from dashboard import fleet_policy as fp


NOW = 1_000_000.0


def _w(worker_id="w1", *, ready=6, total=6, active=0, draining=False,
       status="ready", role="", hb_age=5, **extra):
    """Build a normalized worker record directly (bypassing hash parsing)."""
    rec = {
        "worker_id": worker_id, "role": role, "status": status,
        "draining": draining, "slots_ready": ready, "slots_total": total,
        "slots_free": max(0, ready - active), "active_jobs": active,
        "warming": status == "warming" or (total > 0 and ready < total),
        "stale": hb_age > 90, "heartbeat_age_s": hb_age, "host": "10.0.0.1",
        "mem_available_mb": 8000, "time_to_ready_s": 120,
    }
    rec.update(extra)
    return rec


def _state(workers, inflight=(), per=6):
    return fp.fleet_state(list(workers), list(inflight), per)


# ── capacity model ──────────────────────────────────────────────────────────

def test_c5_model_reproduces_production_reality():
    # The only real validation available: production runs 6 on a c5.2xlarge,
    # and the 12-session load test failed on memory pressure.
    assert fp.slots_for_instance("c5.2xlarge") == 6
    assert fp.memory_slots("c5.2xlarge", safety=1.0) == 7
    assert fp.limiting_factor("c5.2xlarge") == "memory"


def test_r6a_memory_ceiling_matches_measured_host_memory():
    # Density box reported 62,919 MiB. Raw arithmetic 37, planned 29.
    assert fp.memory_slots("r6a.2xlarge") == 29
    assert fp.memory_slots("r6a.2xlarge", safety=1.0) == 37


def test_r6a_is_cpu_bound_not_memory_bound():
    # MEASURED 2026-08-12: 30 full-lab sessions on one r6a.2xlarge left memory
    # comfortable (41.7 GiB of 62.9, pressure ~0%) while CPU pressure held 98%
    # for 18 minutes and every install missed the framework's 600s readiness
    # gate. Planning on memory alone would oversell this shape ~2x.
    assert fp.cpu_slots("r6a.2xlarge") == 16
    assert fp.slots_for_instance("r6a.2xlarge") == 16
    assert fp.limiting_factor("r6a.2xlarge") == "cpu"


def test_planning_takes_the_lowest_of_all_three_ceilings():
    # Was "lower of both" until 2026-08-13, when a 30-session run on a shape
    # memory and CPU both cleared returned 0 passes — the volume was the wall.
    for t in fp.INSTANCE_MEMORY_MB:
        if fp.slots_for_instance(t):
            assert fp.slots_for_instance(t) == min(
                fp.memory_slots(t), fp.cpu_slots(t), fp.disk_slots()), t


def test_more_memory_than_cores_can_feed_is_wasted():
    # r6a.4xlarge has twice the RAM of m6a.4xlarge and the same core count, so
    # it buys no extra sessions — the cost model must not pretend otherwise.
    assert fp.memory_slots("r6a.4xlarge") > fp.memory_slots("m6a.4xlarge")
    assert fp.slots_for_instance("r6a.4xlarge") >= fp.slots_for_instance("m6a.4xlarge")
    eu = fp.ON_DEMAND_USD_PER_HOUR["eu-west-2"]
    per_session = lambda t: eu[t] / fp.slots_for_instance(t)   # noqa: E731
    assert per_session("m6a.4xlarge") < per_session("r6a.4xlarge")


def test_anything_beats_compute_optimized():
    # The cost thesis survives the CPU correction: c5 is still worst.
    ranked = fp.rank_by_cost("eu-west-2")
    assert ranked[-1]["instance_type"] == "c5.2xlarge"
    # 0.59x on the unit model (m6a 0.0400 vs c5 0.0673). The gap narrowed when
    # planning moved off the four-ceiling arithmetic, because that arithmetic
    # penalised the bigger shape for sharing one volume — a start-rate limit
    # that paced admission now handles, not a holding limit.
    assert ranked[0]["usd_per_session_hour"] < 0.7 * ranked[-1]["usd_per_session_hour"]


def test_rank_by_cost_prices_deliverable_slots_not_installed_ram():
    for row in fp.rank_by_cost("eu-west-2"):
        # Prices what you are PLANNED to get, not the ceiling arithmetic. The
        # ceilings are still reported alongside, because "raising IOPS would
        # buy seats" is advice an operator can act on.
        assert row["slots"] == fp.planned_slots(row["instance_type"])
        assert row["memory_slots"] > 0 and row["cpu_slots"] > 0
        assert row["binds"] in ("memory", "cpu", "disk-bandwidth", "disk-iops",
                                "balanced")


def test_unknown_instance_type_refuses_rather_than_guesses():
    # Guessing high is how a class gets oversold.
    assert fp.slots_for_instance("wat.9xlarge") == 0
    assert fp.instances_for_seats(70, "wat.9xlarge") == 0


def test_instances_for_seats_includes_redundancy():
    per = fp.planned_slots("r6a.2xlarge")               # 10 units
    assert per == 10
    assert fp.instances_for_seats(70, "r6a.2xlarge") == 7 + 1
    assert fp.instances_for_seats(70, "r6a.2xlarge", redundancy=False) == 7
    # N+1 must genuinely cover the loss of one host.
    assert (fp.instances_for_seats(70, "r6a.2xlarge") - 1) * per >= 70


def test_seats_zero_needs_nothing():
    assert fp.instances_for_seats(0, "r6a.2xlarge") == 0


# ── worker normalization ────────────────────────────────────────────────────

def test_normalize_prefers_slots_ready_over_capacity():
    w = fp.normalize_worker("w1", {
        "capacity": "3", "slots_ready": "3", "slots_total": "12",
        "active_jobs": "1", "slots_free": "2", "status": "warming",
    }, NOW)
    assert w["slots_ready"] == 3 and w["slots_total"] == 12
    assert w["warming"] is True and w["slots_free"] == 2


def test_normalize_falls_back_for_old_agents():
    # During a rolling deploy a worker still on the old code publishes only
    # `capacity`. It must still be readable rather than counted as zero.
    w = fp.normalize_worker("old", {"capacity": "6", "active_jobs": "2"}, NOW)
    assert w["slots_ready"] == 6
    assert w["slots_free"] == 4


def test_normalize_detects_stale_heartbeat():
    from datetime import datetime, timezone
    old = datetime.fromtimestamp(NOW - 600, tz=timezone.utc).isoformat()
    fresh = datetime.fromtimestamp(NOW - 5, tz=timezone.utc).isoformat()
    assert fp.normalize_worker("a", {"last_heartbeat": old}, NOW)["stale"] is True
    assert fp.normalize_worker("b", {"last_heartbeat": fresh}, NOW)["stale"] is False


def test_normalize_draining_spellings():
    for raw in ("1", "true", "YES"):
        assert fp.normalize_worker("w", {"draining": raw}, NOW)["draining"] is True
    for raw in ("0", "", None):
        assert fp.normalize_worker("w", {"draining": raw}, NOW)["draining"] is False


# ── fleet state ─────────────────────────────────────────────────────────────

def test_free_effective_counts_inflight_launches():
    # THE anti-runaway term. Without it the same deficit is re-decided every
    # tick for the whole boot time.
    st = _state([_w(ready=0, total=6, status="warming")],
                inflight=[{"expected_slots": 6}, {"expected_slots": 6}], per=6)
    assert st["free_ready"] == 0
    assert st["free_inflight"] == 12
    assert st["free_effective"] == 12
    assert st["inflight_launches"] == 2


def test_draining_worker_contributes_no_capacity():
    st = _state([_w("a"), _w("b", draining=True)])
    assert st["free_effective"] == 6
    assert st["workers_draining"] == 1


def test_stale_worker_contributes_no_capacity():
    st = _state([_w("a"), _w("dead", hb_age=600)])
    assert st["free_effective"] == 6
    assert st["workers_stale"] == 1


def test_master_is_never_counted_as_fleet_capacity():
    # The master runs Redis/nginx/FastAPI; it is not a scale target.
    st = _state([_w("master-arm64", role="master", ready=5), _w("a")])
    assert st["free_effective"] == 6


def test_warming_worker_contributes_only_what_it_has_warmed():
    # This is the whole point of the agent-side honesty fix.
    st = _state([_w(ready=4, total=12, status="warming")])
    assert st["free_effective"] == 4
    assert st["workers_warming"] == 1


def test_occupied_slots_are_not_free():
    st = _state([_w(ready=6, active=6)])
    assert st["slots_ready"] == 6
    assert st["free_effective"] == 0


# ── scale up ────────────────────────────────────────────────────────────────

def test_no_action_when_target_already_met():
    plan = fp.plan_scale_up(6, _state([_w()]), instance_type="c5.2xlarge")
    assert plan["action"] == "none" and plan["launch"] == 0


def test_launch_count_derived_from_measured_density():
    # 70 seats, nothing running, 10 planned slots per r6a.2xlarge → 7.
    plan = fp.plan_scale_up(70, _state([]), instance_type="r6a.2xlarge",
                            per_call_cap=10)
    assert plan["action"] == "launch"
    assert plan["launch"] == 7
    assert plan["per_instance"] == 10


def test_undrain_is_preferred_over_launching():
    # Reclaiming a cordoned worker is instant and free; buying one is minutes
    # and billed. The cordoned worker must be used first.
    cordoned = _w("cordoned", ready=6, draining=True)
    st = _state([_w("a", ready=6, active=6), cordoned])
    plan = fp.plan_scale_up(6, st, instance_type="c5.2xlarge",
                            workers_draining=[cordoned])
    assert plan["action"] == "undrain"
    assert plan["undrain"] == ["cordoned"]
    assert plan["launch"] == 0


def test_undrain_then_launch_for_the_remainder():
    cordoned = _w("cordoned", ready=6, draining=True)
    st = _state([cordoned])
    plan = fp.plan_scale_up(20, st, instance_type="c5.2xlarge",
                            workers_draining=[cordoned], per_call_cap=10)
    assert plan["undrain"] == ["cordoned"]
    assert plan["launch"] == 3          # 20 - 6 = 14 short, ceil(14/6) = 3
    assert plan["action"] == "launch"


def test_per_call_cap_limits_launch():
    plan = fp.plan_scale_up(200, _state([]), instance_type="c5.2xlarge",
                            per_call_cap=4, max_fleet=100)
    assert plan["launch"] == 4
    assert plan["capped_by"] == "per_call_cap"
    assert "wanted" in plan["reason"]


def test_max_fleet_ceiling_counts_inflight_too():
    # An instance that is booting still occupies a slot in the ceiling.
    st = _state([_w("a"), _w("b")], inflight=[{"expected_slots": 6}])
    plan = fp.plan_scale_up(200, st, instance_type="c5.2xlarge",
                            max_fleet=5, per_call_cap=10)
    assert plan["launch"] == 2         # 5 - 2 existing - 1 inflight
    assert plan["capped_by"] == "max_fleet"


def test_vcpu_quota_can_be_the_binding_cap():
    plan = fp.plan_scale_up(200, _state([]), instance_type="c5.2xlarge",
                            per_call_cap=10, max_fleet=100,
                            quota_headroom_instances=2)
    assert plan["launch"] == 2
    assert plan["capped_by"] == "vcpu_quota"


def test_frozen_fleet_refuses_everything():
    try:
        fp.plan_scale_up(70, _state([]), frozen=True)
    except fp.PolicyRefusal as e:
        assert "frozen" in str(e)
    else:
        raise AssertionError("expected PolicyRefusal")


def test_unknown_instance_type_refuses_to_plan():
    try:
        fp.plan_scale_up(70, _state([]), instance_type="wat.9xlarge")
    except fp.PolicyRefusal as e:
        assert "unknown instance type" in str(e)
    else:
        raise AssertionError("expected PolicyRefusal")


def test_zero_headroom_refuses_rather_than_launching_nothing_silently():
    try:
        fp.plan_scale_up(70, _state([]), instance_type="c5.2xlarge",
                         max_fleet=0)
    except fp.PolicyRefusal as e:
        assert "guardrail allows 0" in str(e)
    else:
        raise AssertionError("expected PolicyRefusal")


# ── scale down ──────────────────────────────────────────────────────────────

def test_scale_down_only_cordons_never_terminates():
    workers = [_w("a"), _w("b"), _w("c")]
    plan = fp.plan_scale_down(6, _state(workers), workers, min_workers=1)
    assert plan["action"] == "cordon"
    assert "terminat" in plan["reason"]      # explicit: only after sessions end
    assert set(plan["cordon"]) <= {"a", "b", "c"}


def test_scale_down_picks_emptiest_first():
    workers = [_w("busy", active=5), _w("empty", active=0), _w("half", active=3)]
    plan = fp.plan_scale_down(0, _state(workers), workers, min_workers=1)
    assert plan["cordon"][0] == "empty"


def test_scale_down_respects_min_worker_floor():
    workers = [_w("a")]
    plan = fp.plan_scale_down(0, _state(workers), workers, min_workers=1)
    assert plan["action"] == "none"
    assert "floor" in plan["reason"]


def test_scale_down_does_nothing_without_surplus():
    workers = [_w("a", active=6)]
    plan = fp.plan_scale_down(70, _state(workers), workers)
    assert plan["action"] == "none"


def test_scale_down_never_cuts_below_target():
    # Two workers, 12 free, target 10 → surplus 2, but removing either frees 6
    # and would drop below target. Refuse rather than strand learners.
    workers = [_w("a"), _w("b")]
    plan = fp.plan_scale_down(10, _state(workers), workers, min_workers=1)
    assert plan["action"] == "none"
    assert "without dropping below target" in plan["reason"]


def test_warming_worker_is_never_a_cordon_candidate():
    # It has no sessions yet, so it looks maximally attractive to cordon —
    # but we just paid minutes of boot time for it.
    workers = [_w("warm", ready=1, total=29, status="warming"),
               _w("a"), _w("b")]
    plan = fp.plan_scale_down(0, _state(workers), workers, min_workers=1)
    assert "warm" not in plan["cordon"]


def test_terminatable_requires_cordoned_and_empty():
    workers = [
        _w("ready-empty", active=0),                      # not cordoned
        _w("cordoned-busy", active=2, draining=True),     # still has sessions
        _w("cordoned-empty", active=0, draining=True),    # ✓
        _w("master", role="master", active=0, draining=True),
    ]
    assert fp.terminatable(workers) == ["cordoned-empty"]


# ── regions & cost ──────────────────────────────────────────────────────────

def test_region_list_is_curated_and_covers_the_bootcamp():
    ids = fp.region_ids()
    assert "ap-southeast-1" in ids           # Singapore — the bootcamp
    assert fp.HOME_REGION in ids             # where the fleet lives today
    assert len(ids) <= 12, "curated short list, not every AWS region"
    assert len(set(ids)) == len(ids)
    assert all(r["area"] for r in fp.REGIONS)


def test_unknown_region_rejected():
    assert fp.is_known_region("ap-southeast-1") is True
    assert fp.is_known_region("mars-north-1") is False


def test_cost_estimate_known_type():
    est = fp.estimate_cost(3, "r6a.2xlarge", 5.0,
                           fp.ON_DEMAND_USD_PER_HOUR["ap-southeast-1"])
    assert est["known"] is True
    assert abs(est["total_usd"] - 8.21) < 0.02      # 3 × 0.5472 × 5
    assert est["daily_usd"] > est["total_usd"]


def test_cost_estimate_unknown_type_is_honest():
    est = fp.estimate_cost(3, "wat.9xlarge", 5.0)
    assert est["known"] is False and est["total_usd"] is None


def test_current_vs_proposed_fleet_cost_and_capacity():
    """The comparison the fleet swap decision rests on."""
    eu = fp.ON_DEMAND_USD_PER_HOUR["eu-west-2"]
    now = fp.estimate_cost(2, "c5.2xlarge", 730.0, eu)
    proposed = fp.estimate_cost(3, "r6a.2xlarge", 730.0, eu)
    now_slots = 2 * 6
    proposed_slots = 3 * fp.slots_for_instance("r6a.2xlarge")

    assert now_slots == 12
    assert proposed_slots == 48
    # Monthly bill roughly doubles...
    assert proposed["total_usd"] > now["total_usd"]
    # ...while cost per slot collapses.
    assert (proposed["total_usd"] / proposed_slots) < 0.55 * (now["total_usd"] / now_slots)

    # 2 x m6a.4xlarge was expected to give 60 seats on the memory+CPU model.
    # The 2026-08-13 disk measurement cut that to 36: the extra RAM and cores
    # are real but stranded behind one stock gp3 volume each.
    #
    # A first correction claimed 500 MB/s of provisioned throughput restored the
    # 60. That was then MEASURED and came out 8/30 — bandwidth was necessary and
    # not sufficient, because IOPS stayed at the 3,000 baseline. 36 stands until
    # a run with both dimensions raised says otherwise.
    balanced = fp.estimate_cost(2, "m6a.4xlarge", 730.0, eu)
    balanced_slots = 2 * fp.slots_for_instance("m6a.4xlarge")
    assert balanced_slots == 36
    # Throughput alone buys nothing: 8/30 measured.
    assert 2 * fp.slots_for_instance("m6a.4xlarge", throughput_mbps=500) == 36
    # Both dimensions raised is what the 60 would need — still a projection.
    assert 2 * fp.slots_for_instance("m6a.4xlarge", throughput_mbps=500, iops=5000) == 60
    assert abs(balanced["total_usd"] - proposed["total_usd"]) < 1.0


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


# --- curated instance picker -------------------------------------------------

def test_instance_choices_are_costed_and_sized():
    choices = fp.instance_choices("eu-west-2")
    assert len(choices) == 4
    for c in choices:
        assert c["slots"] > 0
        assert c["usd_per_session_hour"] > 0
        assert c["best_for"] and c["why"]
        assert c["limited_by"] in ("memory", "cpu", "disk", "balanced")


def test_exactly_one_recommended_and_it_is_the_cheapest_per_session():
    choices = fp.instance_choices("eu-west-2")
    rec = [c for c in choices if c["recommended"]]
    assert len(rec) == 1, "an ambiguous recommendation is worse than none"
    cheapest = min(choices, key=lambda c: c["usd_per_session_hour"])
    assert rec[0]["type"] == cheapest["type"]
    # A 4xlarge is worth exactly two 2xlarges and costs exactly twice as much,
    # so the two tie on cost per session. min() breaks the tie by list order and
    # picks the 4xlarge, which is the right default: half as many machines to
    # warm, register and tear down for the same money.
    assert rec[0]["type"] == "m6a.4xlarge"
    two = {c["type"]: c for c in choices}["m6a.2xlarge"]
    assert rec[0]["usd_per_session_hour"] == two["usd_per_session_hour"]


def test_recommendation_is_derived_from_the_unit_table_not_a_hardcoded_flag():
    """A stale hardcoded flag is exactly what promised 30 seats that a
    30-session run could not deliver. The recommendation must move when the
    measured capacity moves, so this changes the table and checks it follows."""
    from dashboard import capacity_units as cu

    original = dict(cu.INSTANCE_UNITS)
    try:
        # Halve the 4xlarge's worth. It now costs twice as much per session as
        # the 2xlarge, and the recommendation must abandon it.
        cu.INSTANCE_UNITS["m6a.4xlarge"] = 10
        derived = [c["type"] for c in fp.instance_choices("eu-west-2")
                   if c["recommended"]]
        assert derived == ["m6a.2xlarge"], derived
    finally:
        cu.INSTANCE_UNITS.clear()
        cu.INSTANCE_UNITS.update(original)

    # And back to the real table, exactly one recommendation.
    assert len([c for c in fp.instance_choices("eu-west-2")
                if c["recommended"]]) == 1


def test_choices_cover_three_families():
    fams = {c["family"] for c in fp.instance_choices("eu-west-2")}
    assert fams == {"General purpose", "Memory optimized", "Compute optimized"}


def test_memory_optimized_is_cpu_limited_for_this_workload():
    """The measured surprise, pinned: r6a has RAM its cores cannot feed."""
    choices = {c["type"]: c for c in fp.instance_choices("eu-west-2")}
    assert choices["r6a.2xlarge"]["limited_by"] == "cpu"
    assert choices["c5.2xlarge"]["limited_by"] == "memory"


def test_choices_survive_a_region_with_no_price_table():
    """A missing price must not hide a valid instance type."""
    choices = fp.instance_choices("us-east-1")
    assert len(choices) == 4
    assert all(c["usd_per_session_hour"] is None for c in choices)
    assert all(c["slots"] > 0 for c in choices)


# ── the disk ceiling (measured 2026-08-13, three runs on one m6a.4xlarge) ────
# N=12 → 29/60 retries used, 12/12 pass | N=20 → 53/60, 20/20 pass
# N=30 → gate exhausted, 0/30 pass. Memory and CPU explain none of that.

def test_disk_ceiling_matches_the_measured_runs():
    # The model has to land between the run that passed with margin and the one
    # that exhausted the gate — and it must not exceed the last passing run.
    n = fp.disk_slots(125)
    assert 12 < n <= 20, f"disk ceiling {n} contradicts the 12-pass / 20-pass / 30-fail runs"
    assert n == 18


def test_disk_ceiling_scales_with_volume_throughput_not_instance_size():
    # The whole practical point: throughput is the cheap lever. 4x the disk
    # must buy materially more than 4 extra seats.
    assert fp.disk_slots(500) > 4 * fp.disk_slots(125)
    assert fp.disk_slots(250) > fp.disk_slots(125)


def test_disk_binds_first_on_the_default_shape():
    # m6a.4xlarge: memory says 30, CPU says 32, disk says 18. Before this term
    # existed the picker promised 30 and a 30-session run returned 0 passes.
    assert fp.memory_slots("m6a.4xlarge") > 18
    assert fp.cpu_slots("m6a.4xlarge") > 18
    assert fp.slots_for_instance("m6a.4xlarge") == 18
    # On a stock gp3 volume both disk dimensions land on 18 at once, so neither
    # is singled out. Raising only one leaves the other at 18 — measured.
    assert fp.limiting_factor("m6a.4xlarge") == "balanced"
    assert fp.disk_slots(fp.DEFAULT_VOLUME_THROUGHPUT_MBPS) == 18
    assert fp.iops_slots(fp.DEFAULT_VOLUME_IOPS) == 18


def test_disk_does_not_blanket_cap_smaller_shapes():
    # A ceiling that binds everywhere is just a constant. These shapes run out
    # of their own resource first and must keep their own limiting factor.
    assert fp.limiting_factor("c5.2xlarge") == "memory"
    assert fp.limiting_factor("m6a.2xlarge") == "memory"
    assert fp.limiting_factor("r6a.2xlarge") == "cpu"


def test_faster_disk_alone_does_not_hand_the_ceiling_back_to_memory():
    """MEASURED 2026-08-13, and it falsified the previous version of this test.

    Both workers were raised 125 → 500 MB/s and 30 sessions were run again:
    8/30 passed, 22 exhausted the gate. The bandwidth pin really did lift
    (523 MB/s peak, up from a hard 125.2) — but IOPS stayed at the 3,000
    baseline and became the next ceiling, at the same 18. Raising one disk
    dimension and not the other buys nothing.
    """
    assert fp.disk_slots(500) > 30                      # bandwidth no longer binds
    assert fp.slots_for_instance("m6a.4xlarge", throughput_mbps=500) == 18
    assert fp.limiting_factor("m6a.4xlarge", throughput_mbps=500) == "disk-iops"


def test_raising_both_disk_dimensions_reaches_the_memory_ceiling():
    """PROJECTION, not a measurement — flagged as such deliberately, because the
    last unmeasured projection in this file was wrong by 22 sessions."""
    assert fp.slots_for_instance("m6a.4xlarge", throughput_mbps=500, iops=5000) == \
        min(fp.memory_slots("m6a.4xlarge"), fp.cpu_slots("m6a.4xlarge"))


def test_iops_and_bandwidth_are_independent_ceilings():
    # Different resources, binding in different phases of the same install:
    # bandwidth while the image is pulled and extracted, IOPS while the
    # ActiveGate JVM starts. Neither may be inferred from the other.
    assert fp.iops_slots(3000) == 18
    assert fp.iops_slots(0) == 0
    assert fp.slots_for_instance("m6a.4xlarge", throughput_mbps=500, iops=0) == 0
    assert fp.limiting_factor("m6a.4xlarge", throughput_mbps=500, iops=0) == "unknown"


def test_zero_or_negative_throughput_refuses_to_plan():
    # Never guess high — an unknown volume must read as "cannot plan", the same
    # contract as an unknown instance type.
    assert fp.disk_slots(0) == 0
    assert fp.slots_for_instance("m6a.4xlarge", throughput_mbps=0) == 0
    assert fp.limiting_factor("m6a.4xlarge", throughput_mbps=0) == "unknown"
