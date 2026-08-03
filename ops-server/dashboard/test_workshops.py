"""Pure-logic tests for EPIC-002 Workshops (dashboard/live_sessions.py
extensions + dashboard/live_pad.py).

No Redis, no FastAPI — same approach as test_live_sessions.py: the /api/live/*
endpoints stay thin and delegate every decision to these functions. Covers
schedule-create initial state, join codes (happy / normalization / seat caps),
the cancel transition + learner visibility, joinCode payload gating, capacity
math from fake worker hashes, pad Q&A assembly, the pad section trainer-gate,
and the export snapshot rendering.

Runnable two ways:
  - pytest:     python3 -m pytest dashboard/test_workshops.py
  - standalone: /home/ops/ops-venv/bin/python -m dashboard.test_workshops
"""

from dashboard import live_pad as lp
from dashboard import live_sessions as ls

TRAINER = "trainer@dynatrace.com"


def _session(state="open", trainer=TRAINER, **extra) -> dict:
    """A live:session:{id} hash as stored in Redis (all-string values)."""
    sess = {
        "title": "K8s 101 — EMEA Bootcamp", "trainingId": "kubernetes-101",
        "ref": "", "trainerEmail": trainer, "state": state,
        "createdAt": "2026-07-14T09:00:00+00:00", "startedAt": "", "endedAt": "",
    }
    sess.update(extra)
    return sess


def _raises_value_error(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"expected ValueError from {fn.__name__}{args}")


# ── Schedule-create: initial state + field validation ────────────────────────

def test_initial_state_scheduled_vs_open():
    assert ls.initial_state("2026-09-01T09:00:00+00:00") == "scheduled"
    assert ls.initial_state("") == "open"       # today's behavior preserved
    assert ls.initial_state(None) == "open"
    assert ls.initial_state("   ") == "open"


def test_validate_schedule_normalizes_and_stringifies():
    out = ls.validate_schedule(" 2026-09-01T09:00:00Z ", " Europe/Madrid ",
                               90, 25)
    assert out == {"scheduledAt": "2026-09-01T09:00:00Z",
                   "timezone": "Europe/Madrid",
                   "durationMinutes": "90", "maxSeats": "25"}


def test_validate_schedule_all_absent_is_fine():
    out = ls.validate_schedule("", "", 0, 0)
    assert out == {"scheduledAt": "", "timezone": "",
                   "durationMinutes": "", "maxSeats": ""}


def test_validate_schedule_rejects_bad_values():
    assert "scheduledAt" in _raises_value_error(
        ls.validate_schedule, "next tuesday", "", 0, 0)
    assert "timezone" in _raises_value_error(
        ls.validate_schedule, "", "Madrid", 0, 0)   # not an IANA name
    assert "durationMinutes" in _raises_value_error(
        ls.validate_schedule, "", "", "ninety", 0)
    assert "maxSeats" in _raises_value_error(
        ls.validate_schedule, "", "", 0, -5)


# ── Join codes ───────────────────────────────────────────────────────────────

def test_generate_join_code_shape_and_alphabet():
    for _ in range(50):
        code = ls.generate_join_code()
        assert len(code) == 6
        assert all(c in ls.JOIN_CODE_ALPHABET for c in code)
    # no confusables in the alphabet: I/L/O and the 0/1 they mimic
    for confusable in "ILO01":
        assert confusable not in ls.JOIN_CODE_ALPHABET


def test_normalize_join_code_case_insensitive():
    assert ls.normalize_join_code(" x7km2q ") == "X7KM2Q"
    assert ls.normalize_join_code("X7KM2Q") == "X7KM2Q"   # same lookup key
    assert ls.normalize_join_code(None) == ""
    assert ls.normalize_join_code("") == ""


def test_join_by_code_happy_in_all_joinable_states():
    roster = {"alice@x.com"}
    for state in ("scheduled", "open", "running"):
        assert ls.join_by_code_error(state, "bob@x.com", roster, 0) is None


def test_join_by_code_rejects_ended_and_cancelled():
    assert ls.join_by_code_error("ended", "bob@x.com", set(), 0) == \
        (409, "session has ended")
    assert ls.join_by_code_error("cancelled", "bob@x.com", set(), 0) == \
        (409, "session has been cancelled")


def test_join_by_code_max_seats_full():
    roster = {"a@x.com", "b@x.com", "c@x.com"}
    status, detail = ls.join_by_code_error("open", "new@x.com", roster, 3)
    assert status == 409
    assert "full" in detail
    # one seat still free → allowed
    assert ls.join_by_code_error("open", "new@x.com", roster, 4) is None
    # maxSeats 0 = unlimited
    assert ls.join_by_code_error("open", "new@x.com", roster, 0) is None


def test_join_by_code_rejoin_never_seat_blocked():
    roster = {"a@x.com", "b@x.com"}
    assert ls.join_by_code_error("open", "A@X.com ", roster, 2) is None


def test_invite_join_allows_scheduled_rejects_cancelled():
    roster = {"alice@x.com"}
    assert ls.join_error("scheduled", "alice@x.com", roster) is None
    assert ls.join_error("cancelled", "alice@x.com", roster) == \
        (409, "session has been cancelled")


# ── Cancel / open-registration transitions ───────────────────────────────────

def test_cancel_from_scheduled_and_open():
    assert ls.apply_transition("scheduled", "cancel") == ("cancelled", True)
    assert ls.apply_transition("open", "cancel") == ("cancelled", True)


def test_cancel_idempotent_and_illegal_states():
    assert ls.apply_transition("cancelled", "cancel") == ("cancelled", False)
    for state in ("running", "ended"):
        try:
            ls.apply_transition(state, "cancel")
            raise AssertionError(f"expected ValueError for cancel from {state}")
        except ValueError:
            pass


def test_open_registration_and_start_from_scheduled():
    assert ls.apply_transition("scheduled", "open-registration") == ("open", True)
    assert ls.apply_transition("open", "open-registration") == ("open", False)
    assert ls.apply_transition("scheduled", "start") == ("running", True)
    try:
        ls.apply_transition("cancelled", "start")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_cancelled_session_still_listed_for_learners():
    """Cancelled keeps entity + index — learners must SEE the cancellation
    (only ended sessions drop out of the list)."""
    roster = {"alice@x.com"}
    cancelled = _session(state="cancelled",
                         cancelledAt="2026-07-14T11:00:00+00:00")
    assert ls.is_listed(cancelled, roster, "alice@x.com")
    item = ls.shape_summary("sid-1", cancelled, roster, {}, "alice@x.com")
    assert item["state"] == "cancelled"
    assert item["cancelledAt"] == "2026-07-14T11:00:00+00:00"
    assert not ls.is_listed(_session(state="ended"), roster, "alice@x.com")


# ── joinCode / workshop-field payload gating ─────────────────────────────────

def test_join_code_hidden_from_learners_shown_to_trainer():
    sess = _session(joinCode="X7KM2Q", maxSeats="20")
    roster = {"alice@x.com"}
    for shape in (ls.shape_summary, ls.shape_detail):
        learner = shape("sid-1", sess, roster, {}, "alice@x.com")
        trainer = shape("sid-1", sess, roster, {}, TRAINER)
        assert "joinCode" not in learner
        assert trainer["joinCode"] == "X7KM2Q"
        assert learner["maxSeats"] == 20      # seat cap is public (ints)
        assert trainer["maxSeats"] == 20


def test_workshop_fields_absent_keeps_legacy_payload_shape():
    """Pre-workshop sessions must serialize byte-identically — no new keys."""
    item = ls.shape_summary("sid-1", _session(), {"alice@x.com"}, {}, "alice@x.com")
    for field in ("scheduledAt", "timezone", "durationMinutes", "maxSeats",
                  "cancelledAt", "joinCode"):
        assert field not in item


def test_workshop_fields_present_when_stored():
    sess = _session(state="scheduled", scheduledAt="2026-09-01T09:00:00Z",
                    timezone="Europe/Madrid", durationMinutes="90")
    out = ls.shape_detail("sid-1", sess, {"alice@x.com"}, {}, "alice@x.com")
    assert out["scheduledAt"] == "2026-09-01T09:00:00Z"
    assert out["timezone"] == "Europe/Madrid"
    assert out["durationMinutes"] == 90


# ── Capacity math ────────────────────────────────────────────────────────────

def test_capacity_summary_from_worker_hashes():
    workers = [  # worker:{id} heartbeat hashes (all-string values)
        {"worker_id": "wamd001", "capacity": "6", "arch": "amd64"},
        {"worker_id": "wamd002", "capacity": "6", "arch": "amd64"},
        {"worker_id": "master", "capacity": "4", "arch": "arm64"},
    ]
    active = {"wamd001": 5, "wamd002": 2}
    out = ls.capacity_summary(workers, active, needed=8)
    assert out["capacity"] == 16
    assert out["active"] == 7
    assert out["available"] == 9
    assert out["needed"] == 8
    assert out["sufficient"] is True
    assert out["workers"] == [
        {"id": "master", "capacity": 4, "active": 0},
        {"id": "wamd001", "capacity": 6, "active": 5},
        {"id": "wamd002", "capacity": 6, "active": 2},
    ]


def test_capacity_summary_insufficient_and_garbage_tolerant():
    workers = [{"worker_id": "w1", "capacity": "2"},
               {"worker_id": "w2", "capacity": "not-a-number"}]
    out = ls.capacity_summary(workers, {"w1": 2}, needed=1)
    assert out == {"capacity": 2, "active": 2, "available": 0, "needed": 1,
                   "sufficient": False,
                   "workers": [{"id": "w1", "capacity": 2, "active": 2},
                               {"id": "w2", "capacity": 0, "active": 0}]}
    empty = ls.capacity_summary([], {}, needed=0)
    assert empty["sufficient"] is True and empty["capacity"] == 0


# ── Readiness classification ─────────────────────────────────────────────────

def test_readiness_state_matches_arena_status_contract():
    assert ls.readiness_state({"worker_id": "queued"}, "") == "queued"
    assert ls.readiness_state({"worker_id": ""}, None) == "queued"
    assert ls.readiness_state({"worker_id": "wamd001"}, "cloning…") == "provisioning"
    assert ls.readiness_state({"worker_id": "wamd001"},
                              "…\nDaemon ready\n") == "ready"


def test_failed_job_email_matching():
    roster = {"alice@x.com"}
    record = {"type": "daemon", "status": "failed", "job_id": "enablement-ab12",
              "nightly_run_id": "enablement-kubernetes-101",
              "requested_by": "Alice@X.com", "finished_at": "2026-07-14T10:00:00+00:00"}
    since = "2026-07-14T09:00:00+00:00"
    assert ls.failed_job_email(record, roster, "kubernetes-101", since) == "alice@x.com"
    # wrong training / not failed / not on roster / stale → no match
    assert ls.failed_job_email(record, roster, "dtwiz-101", since) is None
    assert ls.failed_job_email({**record, "status": "completed"}, roster,
                               "kubernetes-101", since) is None
    assert ls.failed_job_email(record, {"bob@x.com"}, "kubernetes-101", since) is None
    assert ls.failed_job_email(record, roster, "kubernetes-101",
                               "2026-07-14T11:00:00+00:00") is None


# ── Cross-tenant join: tenant capture + provision/readiness decisions ────────

SRO = "https://sro97894.apps.dynatrace.com"
COE = "https://geu80787.apps.dynatrace.com"


def test_normalize_tenant_trims_lowercases_and_strips_slash():
    assert ls.normalize_tenant("  HTTPS://SRO97894.Apps.Dynatrace.com/  ") == SRO
    assert ls.normalize_tenant(SRO + "///") == SRO
    assert ls.normalize_tenant("") == ""
    assert ls.normalize_tenant(None) == ""


def test_normalize_tenant_strips_the_runtime_suffix():
    """TEN-1: app FUNCTIONS see https://sro97894-1.apps…, the browser sees
    https://sro97894.apps… — same tenant, and they must compare equal or
    provision-all reports a false foreign-tenant skip."""
    assert ls.normalize_tenant("https://sro97894-1.apps.dynatrace.com") == SRO
    assert ls.normalize_tenant("https://GEU80787-12.apps.dynatrace.com/") == COE
    assert ls.normalize_tenant("https://sro97894-1.apps.dynatrace.com") == ls.normalize_tenant(SRO)
    # only the host's numeric suffix goes — a hyphenated tenant name stays intact
    assert ls.normalize_tenant("https://my-tenant.apps.dynatrace.com") == "https://my-tenant.apps.dynatrace.com"


def test_provision_skip_tolerates_mixed_runtime_forms():
    assert ls.provision_skip_status(True, "https://sro97894-1.apps.dynatrace.com", SRO) is None
    assert ls.readiness_gap_state(True, "https://sro97894-1.apps.dynatrace.com", SRO) == "none"
    # a genuinely different tenant is still foreign
    assert ls.provision_skip_status(True, "https://sro97894-1.apps.dynatrace.com", COE) == "foreign-tenant"


def test_provision_skip_same_tenant_provisions():
    assert ls.provision_skip_status(True, SRO, SRO) is None
    # trailing slash / case variance must not block provisioning
    assert ls.provision_skip_status(True, SRO + "/", SRO.upper()) is None


def test_provision_skip_foreign_tenant():
    assert ls.provision_skip_status(True, SRO, COE) == "foreign-tenant"


def test_provision_skip_not_joined():
    assert ls.provision_skip_status(False, "", COE) == "not-joined"
    # never joined even with a stale tenant record → still not-joined
    assert ls.provision_skip_status(False, SRO, COE) == "not-joined"


def test_provision_skip_backward_compatible_when_tenant_absent():
    # pre-fix join (no tenant recorded) or legacy caller (no workshop tenant):
    # keep the old behavior — provision.
    assert ls.provision_skip_status(True, "", COE) is None
    assert ls.provision_skip_status(True, SRO, "") is None


def test_readiness_gap_state_with_trainer_tenant():
    assert ls.readiness_gap_state(False, "", COE) == "not-joined"
    assert ls.readiness_gap_state(True, SRO, COE) == "foreign"
    assert ls.readiness_gap_state(True, COE, COE) == "none"
    # joined pre-fix (tenant unrecorded) → not provably foreign → none
    assert ls.readiness_gap_state(True, "", COE) == "none"


def test_readiness_gap_state_legacy_without_trainer_tenant():
    # legacy app (no tenant param) keeps the original "none" contract
    assert ls.readiness_gap_state(False, "", "") == "none"
    assert ls.readiness_gap_state(True, SRO, "") == "none"


# ── Pad: question hygiene + section gate ─────────────────────────────────────

def test_clean_text_strips_html_and_caps_length():
    assert lp.clean_text("  <b>How</b> do I <script>x</script>scale?  ") == \
        "How do I xscale?"
    assert lp.clean_text("a" * lp.QUESTION_MAX_CHARS) == "a" * 2000
    assert "2000" in _raises_value_error(lp.clean_text, "a" * 2001)
    assert "required" in _raises_value_error(lp.clean_text, "  <p></p>  ")


def test_section_error_trainer_gate_and_keys():
    sess = _session()
    assert lp.section_error("welcome", TRAINER, sess) is None
    assert lp.section_error("solutions", " Trainer@Dynatrace.COM ", sess) is None
    status, detail = lp.section_error("welcome", "learner@x.com", sess)
    assert status == 403 and "trainer" in detail
    status, detail = lp.section_error("notes", TRAINER, sess)
    assert status == 400 and "welcome" in detail


def test_validate_role():
    assert lp.validate_role(" Trainer ") == "trainer"
    assert lp.validate_role("learner") == "learner"
    _raises_value_error(lp.validate_role, "admin")
    _raises_value_error(lp.validate_role, "")


# ── Pad: Q&A assembly from stream entries ────────────────────────────────────

def _entries():
    """live:pad:{id}:qa stream entries as XRANGE returns them."""
    return [
        ("1-0", {"type": "question", "qid": "", "email": "alice@x.com",
                 "name": "Alice", "text": "Why is the pod pending?",
                 "ts": "2026-07-14T10:00:00+00:00"}),
        ("2-0", {"type": "question", "qid": "", "email": "bob@x.com",
                 "name": "Bob", "text": "How do I get the token?",
                 "ts": "2026-07-14T10:01:00+00:00"}),
        ("3-0", {"type": "answer", "qid": "1-0", "email": TRAINER,
                 "name": "Trainer", "text": "No schedulable node yet.",
                 "ts": "2026-07-14T10:02:00+00:00"}),
        ("4-0", {"type": "answer", "qid": "1-0", "email": TRAINER,
                 "name": "Trainer", "text": "Fixed by the taint step.",
                 "ts": "2026-07-14T10:03:00+00:00"}),
        # answer to an unknown question → dropped, never shown detached
        ("5-0", {"type": "answer", "qid": "9-9", "email": TRAINER,
                 "name": "Trainer", "text": "orphan", "ts": "…"}),
    ]


def test_assemble_qa_nests_answers_under_questions():
    qa = lp.assemble_qa(_entries())
    assert [q["qid"] for q in qa] == ["1-0", "2-0"]
    first = qa[0]
    assert first["name"] == "Alice"
    assert first["text"] == "Why is the pod pending?"
    assert [a["text"] for a in first["answers"]] == \
        ["No schedulable node yet.", "Fixed by the taint step."]
    assert first["answers"][0] == {"name": "Trainer",
                                   "text": "No schedulable node yet.",
                                   "ts": "2026-07-14T10:02:00+00:00"}
    assert qa[1]["answers"] == []
    assert "orphan" not in str(qa)


def test_assemble_qa_empty_and_shape_pad_sections_always_present():
    assert lp.assemble_qa([]) == []
    assert lp.assemble_qa(None) == []
    pad = lp.shape_pad({}, [])
    assert pad == {"sections": {"welcome": "", "solutions": ""}, "qa": []}
    pad = lp.shape_pad({"welcome": "# Hi"}, _entries())
    assert pad["sections"]["welcome"] == "# Hi"
    assert len(pad["qa"]) == 2


# ── Pad: export snapshot (rendered on the end/cancel transition) ─────────────

def test_render_export_contains_sections_and_full_qa():
    sess = _session(state="ended")
    html_doc = lp.render_export(
        sess, {"welcome": "# Welcome\n**read this**", "solutions": "kubectl get pods"},
        lp.assemble_qa(_entries()))
    for expected in ("K8s 101 — EMEA Bootcamp", "kubernetes-101",
                     "Welcome", "Solutions", "kubectl get pods",
                     "Why is the pod pending?", "No schedulable node yet.",
                     "Alice", "Trainer"):
        assert expected in html_doc
    assert html_doc.startswith("<!DOCTYPE html>")
    assert "orphan" not in html_doc


def test_render_export_escapes_html_and_handles_empty_pad():
    sess = _session(title="<script>alert(1)</script>")
    html_doc = lp.render_export(
        sess, {}, lp.assemble_qa([("1-0", {"type": "question", "qid": "",
                                           "email": "e@x", "name": "<img>",
                                           "text": "<svg onload=x>", "ts": ""})]))
    assert "<script>" not in html_doc
    assert "<svg" not in html_doc
    assert "&lt;svg onload=x&gt;" in html_doc
    empty = lp.render_export(_session(), {}, [])
    assert "No questions were asked." in empty


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all workshop tests passed")


def test_end_from_scheduled_raises_value_error():
    """`end` on a scheduled session must raise (endpoint maps it to 409, not 500).

    Regression: the live end endpoint 500'd when a trainer ended a workshop
    that was never started — caught during the 8-bot herd mini-test.
    """
    import pytest as _pytest
    with _pytest.raises(ValueError):
        ls.apply_transition("scheduled", "end")


# ── RFE-C: chat + attendee rail ───────────────────────────────────────────────

def _chat(mid, text, email="l@x.com", name="Lea", role="learner", ts="2026-08-03T10:00:00+00:00"):
    return (mid, {"email": email, "name": name, "role": role, "text": text, "ts": ts})


def test_assemble_chat_keeps_arrival_order_and_marks_pins():
    msgs = lp.assemble_chat([_chat("1-0", "first"), _chat("2-0", "second")],
                            pins={"2-0"})
    assert [m["text"] for m in msgs] == ["first", "second"]
    assert [m["pinned"] for m in msgs] == [False, True]
    assert msgs[0]["mid"] == "1-0"


def test_assemble_chat_clear_watermark_hides_everything_up_to_it():
    entries = [_chat("1-0", "before"), _chat("2-0", "at"), _chat("3-0", "after")]
    msgs = lp.assemble_chat(entries, cleared_before="2-0")
    assert [m["text"] for m in msgs] == ["after"]
    # No watermark = nothing hidden.
    assert len(lp.assemble_chat(entries)) == 3


def test_assemble_chat_compares_stream_ids_numerically_not_lexically():
    """'10-0' is AFTER '9-0'. A string compare would keep message 10 visible
    after a clear at 9 — the whole point of the watermark."""
    entries = [_chat("9-0", "nine"), _chat("10-0", "ten")]
    assert lp.assemble_chat(entries, cleared_before="10-0") == []
    assert [m["text"] for m in lp.assemble_chat(entries, cleared_before="9-0")] == ["ten"]


def test_assemble_chat_sequence_component_orders_within_the_same_ms():
    entries = [_chat("5-1", "a"), _chat("5-2", "b"), _chat("5-10", "c")]
    kept = [m["text"] for m in lp.assemble_chat(entries, cleared_before="5-2")]
    assert kept == ["c"]


def test_assemble_chat_tolerates_junk_ids_and_empty_input():
    assert lp.assemble_chat([]) == []
    assert lp.assemble_chat(None) == []
    # A junk id parses to (0, 0) — it must not raise.
    assert len(lp.assemble_chat([_chat("junk", "x")])) == 1


def test_rate_limit_error_allows_the_nth_and_refuses_the_next():
    assert lp.rate_limit_error(1) is None
    assert lp.rate_limit_error(lp.CHAT_RATE_LIMIT) is None
    status, detail = lp.rate_limit_error(lp.CHAT_RATE_LIMIT + 1)
    assert status == 429
    assert str(lp.CHAT_RATE_LIMIT) in detail


def test_clean_chat_strips_html_and_enforces_the_shorter_cap():
    assert lp.clean_chat("  <b>hi</b>  ") == "hi"
    lp.clean_chat("x" * lp.CHAT_MAX_CHARS)          # at the cap is fine
    for bad in ("", "   ", "<b></b>", "x" * (lp.CHAT_MAX_CHARS + 1)):
        try:
            lp.clean_chat(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_clean_text_keeps_the_longer_question_cap():
    """Chat's cap must not have narrowed Q&A — the two share the function."""
    assert lp.CHAT_MAX_CHARS < lp.QUESTION_MAX_CHARS
    lp.clean_text("x" * lp.QUESTION_MAX_CHARS)


def test_is_present_window_naive_aware_and_junk():
    from datetime import datetime, timedelta, timezone as tz
    now = datetime(2026, 8, 3, 10, 0, tzinfo=tz.utc)
    fresh = (now - timedelta(seconds=10)).isoformat()
    stale = (now - timedelta(seconds=lp.PRESENCE_WINDOW_SECONDS + 1)).isoformat()
    assert lp.is_present(fresh, now) is True
    assert lp.is_present(stale, now) is False
    # Naive timestamps are read as UTC rather than crashing on the subtraction.
    assert lp.is_present((now - timedelta(seconds=5)).replace(tzinfo=None).isoformat(), now) is True
    for junk in ("", None, "not-a-date"):
        assert lp.is_present(junk, now) is False
    # A heartbeat from the future (clock skew) is not "present".
    assert lp.is_present((now + timedelta(seconds=300)).isoformat(), now) is False


def test_display_name_falls_back_to_the_local_part_never_the_address():
    assert lp.display_name("Lea", "lea@x.com") == "Lea"
    assert lp.display_name("", "lea@x.com") == "lea"
    assert lp.display_name("  ", "lea@x.com") == "lea"
    assert "@" not in lp.display_name("", "lea@x.com")


def _presence(email_to_ts):
    import json as _json
    return {e: _json.dumps({"name": "", "role": "learner", "ts": t})
            for e, t in email_to_ts.items()}


def test_shape_attendees_always_has_a_trainer_row_even_with_nobody_joined():
    rows = lp.shape_attendees({}, {}, _session(), {})
    assert [r["email"] for r in rows] == [TRAINER]
    assert rows[0]["role"] == "trainer"
    assert rows[0]["present"] is False


def test_shape_attendees_sorts_trainer_then_present_then_name():
    from datetime import datetime, timezone as tz
    now = datetime(2026, 8, 3, 10, 0, tzinfo=tz.utc)
    joined = {"zoe@x.com": "t", "amy@x.com": "t", "bob@x.com": "t"}
    presence = _presence({"zoe@x.com": now.isoformat()})
    rows = lp.shape_attendees(joined, {}, _session(), presence, now)
    assert [r["email"] for r in rows] == [TRAINER, "zoe@x.com", "amy@x.com", "bob@x.com"]
    assert rows[1]["present"] is True and rows[2]["present"] is False


def test_shape_attendees_carries_the_joining_tenant_and_join_time():
    rows = lp.shape_attendees({"amy@x.com": "2026-08-03T09:00:00+00:00"},
                              {"amy@x.com": "https://sro97894.apps.dynatrace.com"},
                              _session(), {})
    amy = [r for r in rows if r["email"] == "amy@x.com"][0]
    assert amy["tenant"] == "https://sro97894.apps.dynatrace.com"
    assert amy["joinedAt"] == "2026-08-03T09:00:00+00:00"
    assert amy["role"] == "learner"


def test_shape_attendees_includes_someone_present_who_never_joined():
    """Code-join workshops (WS-2) put people in the room without a roster
    entry — the rail must still show them."""
    from datetime import datetime, timezone as tz
    now = datetime(2026, 8, 3, 10, 0, tzinfo=tz.utc)
    rows = lp.shape_attendees({}, {}, _session(), _presence({"walkin@x.com": now.isoformat()}), now)
    assert "walkin@x.com" in [r["email"] for r in rows]


def test_shape_attendees_normalizes_case_so_one_person_is_one_row():
    rows = lp.shape_attendees({"AMY@X.com": "t"}, {}, _session(),
                              _presence({"amy@x.com": ""}))
    assert len([r for r in rows if r["email"] == "amy@x.com"]) == 1


def test_shape_attendees_tolerates_a_bare_iso_presence_value():
    """Heartbeats written by an older build were plain ISO strings."""
    from datetime import datetime, timezone as tz
    now = datetime(2026, 8, 3, 10, 0, tzinfo=tz.utc)
    rows = lp.shape_attendees({"amy@x.com": "t"}, {}, _session(),
                              {"amy@x.com": now.isoformat()}, now)
    assert [r for r in rows if r["email"] == "amy@x.com"][0]["present"] is True


def test_render_export_includes_the_chat_transcript_and_escapes_it():
    html_doc = lp.render_export(
        _session(), {}, [],
        [{"mid": "1-0", "name": "<img src=x>", "email": "l@x.com",
          "text": "<script>alert(1)</script>", "ts": "2026-08-03T10:00", "pinned": True}])
    assert "<h2>Chat</h2>" in html_doc
    assert "<script>alert(1)</script>" not in html_doc
    assert "&lt;script&gt;" in html_doc
    assert "pinned" in html_doc


def test_render_export_says_so_when_the_chat_is_empty():
    assert "No chat messages." in lp.render_export(_session(), {}, [], [])
    # Callers that predate chat (positional 3-arg) must still work.
    assert "No chat messages." in lp.render_export(_session(), {}, [])
