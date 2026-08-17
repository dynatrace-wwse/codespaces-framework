"""Pure-logic tests for the workshop progress proxy (dashboard/live_progress.py).

No Redis, no HTTP, no Grail — exercises only the DQL builder and the record
folding that GET /api/live/sessions/{id}/progress delegates to.

Runnable two ways:
  - pytest:     python3 -m pytest dashboard/test_live_progress.py
  - standalone: /home/ops/ops-venv/bin/python -m dashboard.test_live_progress
"""

from datetime import datetime, timezone

from dashboard import live_progress as lp

SINCE = "2026-08-03T09:00:00Z"


def _rec(event_type, email, **extra):
    rec = {"eventType": event_type, "userEmail": email,
           "timestamp": "2026-08-03T10:00:00Z", "sourceTenant": "https://sro97894.apps.dynatrace.com"}
    rec.update(extra)
    return rec


# ── trainingKey parity with the app ──────────────────────────────────────────

def test_training_key_collapses_both_namespaces():
    assert lp.training_key("kubernetes-101") == "kubernetes-101"
    assert lp.training_key("enablement-kubernetes-101") == "kubernetes-101"
    assert lp.training_key("lab-enablement-kubernetes-101") == "kubernetes-101"
    assert lp.training_key("  Enablement-K8s-101 ") == "k8s-101"
    assert lp.training_key(None) == ""


def test_build_query_normalizes_the_training_id_it_is_given():
    # A workshop stores the catalog id; events carry trainingKey. Passing the
    # repo name must still match.
    q = lp.build_progress_query("ws-1", "enablement-kubernetes-101", ["a@x.com"], SINCE)
    assert 'trainingKey == "kubernetes-101"' in q


# ── Query time window ────────────────────────────────────────────────────────

def test_since_anchors_on_start_minus_grace():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    out = lp.since_timestamp({"startedAt": "2026-08-03T10:00:00Z",
                              "createdAt": "2026-08-01T10:00:00Z"}, now=now)
    assert out == "2026-08-03T08:00:00Z"


def test_since_falls_back_to_creation_not_to_the_schedule():
    """An unstarted workshop anchors on when it was CREATED.

    scheduledAt is a plan, not evidence of activity, and using it was the root of
    the 50%-before-the-start bug: on a not-yet-started workshop it is in the
    future, the old code detected that and fell back to a 72-hour window, and the
    roster arm of the query then swept three days of ordinary self-paced work.
    A workshop cannot have activity older than itself, so creation is the bound.
    """
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    # No grace window before creation — there is nothing to be early for yet.
    assert lp.since_timestamp({"createdAt": "2026-08-03T09:30:00+00:00"}, now=now) \
        == "2026-08-03T09:30:00Z"
    # A schedule alone tells us nothing about activity; fall to the safety floor.
    assert lp.since_timestamp({"scheduledAt": "2026-08-03T11:00:00Z"}, now=now) \
        == "2026-07-31T12:00:00Z"


def test_since_is_clamped_and_never_unbounded():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    floor = "2026-07-31T12:00:00Z"  # now - 72h
    # An old workshop must not turn into a month-wide Grail scan.
    assert lp.since_timestamp({"startedAt": "2026-06-01T10:00:00Z"}, now=now) == floor
    # Neither may a session with no usable timestamps.
    assert lp.since_timestamp({}, now=now) == floor
    assert lp.since_timestamp({"createdAt": "not-a-date"}, now=now) == floor


# ── DQL escaping (the injection boundary) ────────────────────────────────────

def test_escape_dql_string_quotes_and_backslashes():
    assert lp.escape_dql_string('a"b') == 'a\\"b'
    assert lp.escape_dql_string("a\\b") == "a\\\\b"
    assert lp.escape_dql_string(None) == ""


def test_escape_dql_string_strips_control_characters():
    # A newline inside a literal would end the line and let the rest be parsed
    # as pipeline syntax.
    assert lp.escape_dql_string('x"\n| drop everything') == 'x\\"| drop everything'


def test_build_query_escapes_every_interpolated_value():
    q = lp.build_progress_query('ws"1', 'k8s"101', ['a"@x.com'], SINCE)
    assert 'ws\\"1' in q and 'k8s\\"101' in q and 'a\\"@x.com' in q
    # No raw quote may survive to close a literal early.
    assert '"ws"1"' not in q


# ── DQL shape ────────────────────────────────────────────────────────────────

def test_build_query_matches_workshop_id_and_roster_fallback():
    q = lp.build_progress_query("ws-1", "kubernetes-101", ["b@x.com", "a@x.com"], SINCE)
    assert 'workshopId == "ws-1"' in q
    # Roster arm catches events fired before the learner joined the workshop.
    assert 'trainingKey == "kubernetes-101"' in q
    assert 'in(lower(userEmail), {"a@x.com", "b@x.com"})' in q  # sorted + deduped
    assert f'from: toTimestamp("{SINCE}")' in q
    assert f"limit {lp.MAX_RECORDS}" in q


def test_build_query_matches_roster_case_insensitively():
    """A learner stamped with the IdP's casing must still be fetched.

    Events carry whatever casing the tenant's IdP returned
    ("Rodrigo.Pascoal@dynatrace.com"), rosters are stored lowercase. Comparing
    raw dropped those rows from the result set entirely, so the board showed the
    learner as "not-started" forever — _aggregate's _email() only normalizes rows
    the query already returned.
    """
    q = lp.build_progress_query("ws-1", "kubernetes-101", ["Rodrigo.Pascoal@Dynatrace.com"], SINCE)
    assert "lower(userEmail)" in q
    assert '"rodrigo.pascoal@dynatrace.com"' in q
    assert "Rodrigo" not in q


def test_build_query_without_roster_is_workshop_only():
    # Code-only workshop nobody has joined yet — no roster arm, and above all no
    # unbounded "every training event on COE" query.
    q = lp.build_progress_query("ws-1", "kubernetes-101", [], SINCE)
    assert 'workshopId == "ws-1"' in q
    assert "in(userEmail" not in q


def test_build_query_without_training_key_is_workshop_only():
    q = lp.build_progress_query("ws-1", "", ["a@x.com"], SINCE)
    assert "in(userEmail" not in q


# ── Folding ──────────────────────────────────────────────────────────────────

def test_roster_member_without_events_is_not_started():
    out = lp.shape_progress([], ["Alice@X.com "])
    assert [r["email"] for r in out["results"]] == ["alice@x.com"]
    assert out["results"][0]["state"] == "not-started"
    assert out["results"][0]["progressPct"] is None
    assert out["summary"] == {"total": 1, "notStarted": 1, "inProgress": 0,
                              "completed": 0, "avgProgressPct": None, "measured": 0}


def test_started_and_steps_give_in_progress_with_percentage():
    out = lp.shape_progress([
        _rec(lp.STARTED, "a@x.com", startedAt="2026-08-03T10:00:00Z"),
        _rec(lp.STEP_COMPLETED, "a@x.com", completedSteps=1, stepCount=4),
        _rec(lp.STEP_COMPLETED, "a@x.com", completedSteps=3, stepCount=4),
    ], ["a@x.com"])
    r = out["results"][0]
    assert r["state"] == "in-progress"
    assert (r["completedSteps"], r["stepCount"], r["progressPct"]) == (3, 4, 75)
    assert out["summary"]["avgProgressPct"] == 75


def test_step_events_never_go_backwards():
    # Events can arrive out of order; the board must show the furthest point.
    out = lp.shape_progress([
        _rec(lp.STEP_COMPLETED, "a@x.com", completedSteps=3, stepCount=4),
        _rec(lp.STEP_COMPLETED, "a@x.com", completedSteps=1, stepCount=4),
    ])
    assert out["results"][0]["completedSteps"] == 3


def test_step_index_used_when_completed_steps_absent():
    out = lp.shape_progress([_rec(lp.STEP_COMPLETED, "a@x.com", stepIndex=2, stepCount=5)])
    assert out["results"][0]["completedSteps"] == 3  # 0-based index → 3 done


def test_completion_wins_and_fills_in_missing_steps():
    out = lp.shape_progress([
        _rec(lp.STEP_COMPLETED, "a@x.com", completedSteps=1, stepCount=4),
        _rec(lp.COMPLETED, "a@x.com", completedAt="2026-08-03T11:00:00Z",
             score=3, maxScore=6, stepCount=4),
    ], ["a@x.com"])
    r = out["results"][0]
    assert r["state"] == "completed"
    assert r["progressPct"] == 100 and r["completedSteps"] == 4
    assert (r["score"], r["maxScore"], r["scorePct"]) == (3, 6, 50)


def test_retake_keeps_earliest_start_and_latest_completion():
    out = lp.shape_progress([
        _rec(lp.STARTED, "a@x.com", startedAt="2026-08-03T09:30:00Z"),
        _rec(lp.COMPLETED, "a@x.com", completedAt="2026-08-03T10:00:00Z", score=2, maxScore=6),
        _rec(lp.STARTED, "a@x.com", startedAt="2026-08-03T10:10:00Z"),
        _rec(lp.COMPLETED, "a@x.com", completedAt="2026-08-03T10:40:00Z", score=6, maxScore=6),
    ])
    r = out["results"][0]
    assert r["startedAt"] == "2026-08-03T09:30:00Z"
    assert r["completedAt"] == "2026-08-03T10:40:00Z"
    assert r["score"] == 6  # the retake's score, not the first attempt's


def test_questions_counted_with_pass_rate():
    out = lp.shape_progress([
        _rec(lp.QUESTION_ANSWERED, "a@x.com", passed=True),
        _rec(lp.QUESTION_ANSWERED, "a@x.com", passed="false"),
        _rec(lp.QUESTION_ANSWERED, "a@x.com", passed="true"),
    ])
    r = out["results"][0]
    assert (r["questionsAnswered"], r["questionsPassed"]) == (3, 2)
    assert r["state"] == "in-progress"


def test_code_joiner_not_on_roster_still_appears():
    out = lp.shape_progress([_rec(lp.STARTED, "walkin@x.com")], ["a@x.com"])
    assert [r["email"] for r in out["results"]] == ["a@x.com", "walkin@x.com"]
    assert out["summary"] == {"total": 2, "notStarted": 1, "inProgress": 1,
                              "completed": 0, "avgProgressPct": None, "measured": 0}


def test_tenant_and_workshop_name_are_carried_through():
    out = lp.shape_progress([
        _rec(lp.STARTED, "a@x.com", workshopId="ws-1", workshopName="EMEA Bootcamp"),
    ])
    r = out["results"][0]
    assert r["tenant"] == "sro97894"      # canonical env id, not the URL
    assert (r["workshopId"], r["workshopName"]) == ("ws-1", "EMEA Bootcamp")


def test_unknown_step_count_yields_null_progress_not_zero():
    # Honest: "started, depth unknown" — not "0% done".
    out = lp.shape_progress([_rec(lp.STEP_COMPLETED, "a@x.com", completedSteps=2)])
    r = out["results"][0]
    assert r["state"] == "in-progress" and r["completedSteps"] == 2
    assert r["stepCount"] is None and r["progressPct"] is None
    assert out["summary"]["measured"] == 0


def test_summary_averages_only_measured_learners():
    out = lp.shape_progress([
        _rec(lp.STEP_COMPLETED, "a@x.com", completedSteps=1, stepCount=2),   # 50%
        _rec(lp.COMPLETED, "b@x.com", completedAt="2026-08-03T11:00:00Z"),   # 100%
        _rec(lp.STARTED, "c@x.com"),                                          # unknown
    ], ["d@x.com"])
    s = out["summary"]
    assert (s["total"], s["notStarted"], s["inProgress"], s["completed"]) == (4, 1, 2, 1)
    assert s["avgProgressPct"] == 75 and s["measured"] == 2


def test_records_with_no_email_are_dropped():
    out = lp.shape_progress([_rec(lp.STARTED, ""), _rec(lp.STARTED, None)])
    assert out["results"] == []


def test_grail_string_scalars_are_coerced():
    out = lp.shape_progress([
        _rec(lp.STEP_COMPLETED, "a@x.com", completedSteps="2", stepCount="4"),
        _rec(lp.COMPLETED, "a@x.com", completedAt="2026-08-03T11:00:00Z",
             score="5.0", maxScore="10"),
    ])
    r = out["results"][0]
    assert (r["completedSteps"], r["stepCount"]) == (4, 4)
    assert (r["score"], r["maxScore"], r["scorePct"]) == (5, 10, 50)


def test_dotted_event_type_field_also_accepted():
    # Defensive: if the DQL ever returns the raw "event.type" name instead of
    # the aliased one, folding must not silently produce empty rows.
    out = lp.shape_progress([{"event.type": lp.STARTED, "userEmail": "a@x.com",
                              "timestamp": "2026-08-03T10:00:00Z"}])
    assert out["results"][0]["state"] == "in-progress"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all live-progress tests passed")


def test_since_timestamp_future_scheduled_at_clamps_to_floor():
    """A scheduled-but-not-started workshop must not produce a FUTURE lower bound.

    Regression for TIMEFRAME_END_BEFORE_START: the query has no explicit `to:`,
    so Grail ends it at now. A `from:` after now made Grail reject the whole
    query and the cohort board 502'd.
    """
    now = datetime(2026, 8, 4, 16, 51, tzinfo=timezone.utc)
    session = {"scheduledAt": "2026-08-12T17:50:00Z", "startedAt": "", "createdAt": "2026-08-04T10:00:00Z"}
    since = lp.since_timestamp(session, now=now)
    # Creation, NOT the 72-hour floor: the old floor is what leaked self-paced
    # work into a workshop that had not started.
    assert since == "2026-08-04T10:00:00Z", since
    assert since < now.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_since_timestamp_started_workshop_uses_started_at():
    """A running workshop still anchors on startedAt minus the grace window."""
    now = datetime(2026, 8, 4, 16, 51, tzinfo=timezone.utc)
    session = {"startedAt": "2026-08-04T14:00:00Z", "scheduledAt": "2026-08-04T14:00:00Z"}
    assert lp.since_timestamp(session, now=now) == "2026-08-04T12:00:00Z"


# ── The 50%-before-the-workshop-started leak (C1) ────────────────────────────
#
# Reported 2026-08-05: a trainer opened the cohort board of a workshop they had
# not started and saw a rostered learner at 50%. Nobody had done anything in that
# workshop. What the board showed was that learner's own earlier self-paced run of
# the same training, swept in by two faults acting together:
#   1. scheduledAt is in the FUTURE before a workshop starts, so the lower bound
#      collapsed to the 72-hour safety floor; and
#   2. the roster arm matches (trainingKey + email) with no workshop predicate,
#      so everything in that window counted as cohort progress.

def _unstarted(**over):
    return {"createdAt": "2026-08-05T09:00:00Z", "scheduledAt": "2026-08-05T15:00:00Z",
            "startedAt": "", **over}


def test_an_unstarted_workshop_does_not_match_self_paced_work():
    q = lp.build_progress_query("ws-1", "kubernetes-101", ["devlove@googlemail.com"],
                                SINCE, started=lp.has_started(_unstarted()))
    assert 'workshopId == "ws-1"' in q
    # No roster arm at all: the only events that can count are stamped with THIS
    # workshop, and before it starts there are none. (trainingKey still appears in
    # the field projection — it is the FILTER that must not mention it.)
    filter_line = next(l for l in q.splitlines() if l.startswith("| filter workshopId"))
    assert "trainingKey" not in filter_line
    assert "devlove@googlemail.com" not in q


def test_a_running_workshop_still_matches_its_roster():
    session = _unstarted(startedAt="2026-08-05T15:02:00Z")
    q = lp.build_progress_query("ws-1", "kubernetes-101", ["devlove@googlemail.com"],
                                SINCE, started=lp.has_started(session))
    assert 'workshopId == "ws-1"' in q
    assert 'trainingKey == "kubernetes-101"' in q
    assert "devlove@googlemail.com" in q


def test_has_started_reads_only_a_real_start():
    assert not lp.has_started(_unstarted())
    assert not lp.has_started({})
    assert not lp.has_started({"startedAt": ""})
    assert not lp.has_started({"scheduledAt": "2026-08-05T15:00:00Z"})   # a plan is not a start
    assert lp.has_started({"startedAt": "2026-08-05T15:02:00Z"})


def test_the_window_of_an_unstarted_workshop_cannot_predate_it():
    # The specific arithmetic that produced the bug: a workshop created this
    # morning, scheduled for this afternoon, read at midday.
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    since = lp.since_timestamp(_unstarted(), now=now)
    assert since == "2026-08-05T09:00:00Z"          # creation
    assert since != "2026-08-02T12:00:00Z"          # NOT now - 72h, the old answer


def test_a_started_workshop_keeps_its_grace_window():
    # Learners routinely open the training a few minutes before the trainer
    # starts; that work is genuinely theirs and must still count.
    now = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)
    session = _unstarted(startedAt="2026-08-05T15:00:00Z")
    assert lp.since_timestamp(session, now=now) == "2026-08-05T13:00:00Z"


# ── Trainers are not the cohort ───────────────────────────────────────────────

def test_trainers_never_appear_on_the_board():
    # WS-4: a trainer runs the lab alongside the class, so they emit exactly
    # the telemetry a learner does. Charting it put the trainer — and, once
    # teams existed, every co-trainer — into the cohort count and the progress
    # average. The board is the class; the people teaching it are not.
    out = lp.shape_progress(
        [_rec(lp.STARTED, "lead@dynatrace.com"),
         _rec(lp.STARTED, "co@dynatrace.com"),
         _rec(lp.STEP_COMPLETED, "co@dynatrace.com", completedSteps="4", stepCount="5"),
         _rec(lp.STARTED, "alice@x.com")],
        ["alice@x.com"],
        ["lead@dynatrace.com", "co@dynatrace.com"])
    assert [r["email"] for r in out["results"]] == ["alice@x.com"]
    assert out["summary"]["total"] == 1


def test_a_trainer_on_their_own_roster_is_still_a_trainer():
    # Some trainers put themselves on the roster. That is not a promotion to
    # learner — the seeded row has to be dropped too, or the board shows them
    # as a permanently "not-started" attendee.
    out = lp.shape_progress([], ["alice@x.com", "Lead@Dynatrace.com"],
                            ["lead@dynatrace.com"])
    assert [r["email"] for r in out["results"]] == ["alice@x.com"]


def test_no_trainer_list_keeps_every_row():
    # Callers that pass no team (older call sites, the standalone runner) must
    # behave exactly as before.
    out = lp.shape_progress([_rec(lp.STARTED, "alice@x.com")], ["alice@x.com"])
    assert [r["email"] for r in out["results"]] == ["alice@x.com"]
