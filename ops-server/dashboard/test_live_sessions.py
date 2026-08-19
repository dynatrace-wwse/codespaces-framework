"""Pure-logic tests for live training sessions (dashboard/live_sessions.py).

No Redis, no FastAPI — exercises only the decision logic the /api/live/*
endpoints delegate to: email normalization + invalid-drop, create validation,
legal/illegal state transitions, roster gating, and learner-vs-trainer
response shaping.

Runnable two ways:
  - pytest:     python3 -m pytest dashboard/test_live_sessions.py
  - standalone: /home/ops/ops-venv/bin/python -m dashboard.test_live_sessions
"""

from dashboard import live_sessions as ls

TRAINER = "trainer@dynatrace.com"


def _session(state="open", trainer=TRAINER, trainers=None, **extra) -> dict:
    """A live:session:{id} hash as stored in Redis (all-string values).

    `trainers` is the stored truth (JSON list); there is no stored trainerEmail.
    Pass `trainers=[...]` for a co-taught workshop.
    """
    sess = {
        "title": "K8s 101 — EMEA Bootcamp", "trainingId": "kubernetes-101",
        "ref": "", "state": state,
        "trainers": ls.encode_trainers(trainers or [trainer]),
        "createdAt": "2026-07-14T09:00:00+00:00", "startedAt": "", "endedAt": "",
    }
    sess.update(extra)
    return sess


# ── Workshop identity (EPIC-007) ─────────────────────────────────────────────

def test_workshop_id_is_prefixed_and_shaped():
    wid = ls.new_workshop_id(now_ms=1754485000000)
    assert wid.startswith("ws_")
    stamp, _, rand = wid[3:].partition("-")
    assert stamp == "mdzz2i4g"          # base36(epoch-ms)
    assert len(rand) == 6               # 3 bytes hex = 24 bits of entropy


def test_workshop_ids_sort_chronologically():
    # base36(epoch-ms) is fixed-width for every realistic timestamp, so string
    # order IS creation order. Toy values are not fixed-width — use real ones.
    ids = [ls.new_workshop_id(now_ms=t) for t in
           (1754485000000, 1754485000001, 1754571400000)]
    assert ids == sorted(ids)


def test_workshop_id_is_recognisable():
    assert ls.is_workshop_id(ls.new_workshop_id()) is True
    # The old generator's shape, and a worker job id, must NOT pass.
    assert ls.is_workshop_id("mshioahd-6059") is False
    assert ls.is_workshop_id("enablement-ade7b1f16803") is False
    assert ls.is_workshop_id(None) is False


def test_two_workshops_minted_in_the_same_millisecond_differ():
    a = ls.new_workshop_id(now_ms=1754485000000)
    b = ls.new_workshop_id(now_ms=1754485000000)
    assert a != b


# ── Trainer team (EPIC-007) ──────────────────────────────────────────────────

def test_creator_is_always_the_lead():
    assert ls.validate_trainers("Lead@X.com", ["b@x.com"]) == \
        ["lead@x.com", "b@x.com"]
    # …even when the caller lists themselves second, or not at all.
    assert ls.validate_trainers("lead@x.com", ["b@x.com", "LEAD@x.com"]) == \
        ["lead@x.com", "b@x.com"]
    assert ls.validate_trainers("lead@x.com", None) == ["lead@x.com"]


def test_trainer_team_is_capped():
    five = ["b@x.com", "c@x.com", "d@x.com", "e@x.com"]
    assert len(ls.validate_trainers("a@x.com", five)) == ls.MAX_TRAINERS
    try:
        ls.validate_trainers("a@x.com", five + ["f@x.com"])
    except ValueError as exc:
        assert "at most 5" in str(exc)
    else:
        raise AssertionError("a 6-trainer team must be rejected")


def test_trainer_team_drops_invalid_and_dedupes():
    assert ls.validate_trainers("a@x.com", ["bad", "B@x.com", "b@x.com"]) == \
        ["a@x.com", "b@x.com"]


def test_every_trainer_on_the_team_is_a_trainer():
    sess = _session(trainers=["lead@x.com", "co@x.com"])
    assert ls.is_trainer("lead@x.com", sess) is True
    assert ls.is_trainer(" CO@X.com ", sess) is True     # case + whitespace
    assert ls.is_trainer("learner@x.com", sess) is False
    assert ls.is_trainer("", sess) is False


def test_lead_trainer_is_the_first_of_the_team():
    assert ls.lead_trainer(_session(trainers=["a@x.com", "b@x.com"])) == "a@x.com"
    assert ls.lead_trainer({}) == ""


def test_a_bare_trainerEmail_still_reads_as_a_team():
    """Deploy-window guard: a record written by the previous build, or created
    between the Redis wipe and the service restart, must not 500 every request
    that touches it."""
    assert ls.trainers_of({"trainerEmail": "Old@X.com"}) == ["old@x.com"]
    assert ls.is_trainer("old@x.com", {"trainerEmail": "old@x.com"}) is True


def test_unreadable_trainers_field_does_not_explode():
    assert ls.trainers_of({"trainers": "{not json"}) == []
    assert ls.trainers_of({"trainers": '"a string"'}) == []
    assert ls.trainers_of(None) == []


def test_shape_summary_exposes_the_whole_team():
    item = ls.shape_summary("sid-1", _session(trainers=[TRAINER, "co@x.com"]),
                            set(), {}, "co@x.com")
    assert item["trainers"] == [TRAINER, "co@x.com"]
    assert item["trainerEmail"] == TRAINER      # derived echo = the lead
    assert item["isTrainer"] is True            # …and a co-trainer IS a trainer


def test_shape_detail_carries_the_callers_own_role():
    """The workshop route resolves role from ONE fetch — so the detail payload
    has to say whether this caller is a trainer and whether they have joined."""
    sess = _session(trainers=[TRAINER, "co@x.com"])
    joined = {"learner@x.com": "2026-08-06T10:00:00+00:00"}
    assert ls.shape_detail("sid-1", sess, {"learner@x.com"}, joined,
                           "co@x.com")["isTrainer"] is True
    learner = ls.shape_detail("sid-1", sess, {"learner@x.com"}, joined,
                              "Learner@X.com")
    assert learner["isTrainer"] is False
    assert learner["hasJoined"] is True


def test_multi_trainer_roster_targets_gives_every_trainer_a_row():
    out = ls.roster_targets({"alice@x.com"},
                            ["lead@x.com", "co@x.com"], include_trainer=True)
    assert out == [("alice@x.com", "learner"),
                   ("lead@x.com", "trainer"), ("co@x.com", "trainer")]


def test_multi_trainer_roster_targets_never_duplicates():
    """A co-trainer who is also on the roster stays ONE row — otherwise
    provision-all queues two environments for the same person."""
    out = ls.roster_targets({"co@x.com"}, ["lead@x.com", "co@x.com"], True)
    assert [e for e, _ in out].count("co@x.com") == 1
    assert dict(out)["co@x.com"] == "learner"


# ── Seat bound (EPIC-007 §8: a bound, NOT a delivery guarantee) ───────────────

def test_max_seats_bound_is_enforced():
    assert ls.validate_schedule("", "", 0, ls.MAX_SEATS)["maxSeats"] == "200"
    try:
        ls.validate_schedule("", "", 0, ls.MAX_SEATS + 1)
    except ValueError as exc:
        assert "<= 200" in str(exc)
    else:
        raise AssertionError("201 seats must be rejected")


def test_duration_has_no_seat_cap_applied_to_it():
    # _FIELD_MAX is per-field; a long workshop is not a 201-seat workshop.
    assert ls.validate_schedule("", "", 600, 0)["durationMinutes"] == "600"


# ── The room gate (EPIC-007) ─────────────────────────────────────────────────

def test_room_is_closed_until_the_trainer_opens_it():
    assert ls.room_open(_session()) is False
    assert ls.room_open(_session(roomOpen="1")) is True
    assert ls.room_open({}) is False


def test_an_ended_workshop_has_no_open_room():
    """End is implicitly room-close: the flag stays set in Redis for the record,
    but nothing may be written to a finished room."""
    assert ls.room_open(_session(state="ended", roomOpen="1")) is False
    assert ls.room_open(_session(state="cancelled", roomOpen="1")) is False


def test_room_closed_reason_says_which_gate_is_shut():
    assert ls.room_closed_reason(_session()) == \
        "the trainer has not opened the room yet"
    assert ls.room_closed_reason(_session(state="ended")) == \
        "this workshop has ended"
    assert ls.room_closed_reason(_session(state="cancelled")) == \
        "this workshop was cancelled"
    assert ls.room_closed_reason(_session(roomOpen="1")) == ""


def test_the_room_gate_is_independent_of_the_start_gate():
    """The two gates are separate on purpose: a learner can be chatting in the
    room while still unable to start an environment."""
    room_only = _session(state="open", roomOpen="1")
    assert ls.room_open(room_only) is True
    assert room_only["state"] != "running"


# ── Email normalization ──────────────────────────────────────────────────────

def test_normalize_email_trims_and_lowercases():
    assert ls.normalize_email("  Alice@Example.COM ") == "alice@example.com"
    assert ls.normalize_email(None) == ""
    assert ls.normalize_email("") == ""


def test_is_valid_email_requires_at():
    assert ls.is_valid_email("a@b")
    assert not ls.is_valid_email("no-at-sign")
    assert not ls.is_valid_email("")


def test_normalize_roster_drops_invalid_and_dedupes():
    roster = ls.normalize_roster(
        ["  Bob@X.com", "bob@x.com", "not-an-email", "", None, "carol@y.com "])
    assert roster == ["bob@x.com", "carol@y.com"]


def test_normalize_roster_empty_inputs():
    assert ls.normalize_roster([]) == []
    assert ls.normalize_roster(None) == []
    assert ls.normalize_roster(["nope", "also nope"]) == []


# ── Create validation ────────────────────────────────────────────────────────

def test_validate_create_normalizes_everything():
    fields = ls.validate_create(
        "  K8s 101 ", " kubernetes-101 ", " Trainer@Dynatrace.COM ",
        ["Alice@X.com", "bad-entry", "alice@x.com"])
    assert fields == {"title": "K8s 101", "trainingId": "kubernetes-101",
                      "trainers": ["trainer@dynatrace.com"],
                      "trainerEmail": "trainer@dynatrace.com",
                      "roster": ["alice@x.com"]}


def test_validate_create_missing_fields():
    for kwargs in (
        dict(title="", training_id="t", trainer_email=TRAINER, roster=["a@b"]),
        dict(title="T", training_id="", trainer_email=TRAINER, roster=["a@b"]),
        dict(title="T", training_id="t", trainer_email="", roster=["a@b"]),
        dict(title="T", training_id="t", trainer_email="no-at", roster=["a@b"]),
    ):
        try:
            ls.validate_create(kwargs["title"], kwargs["training_id"],
                               kwargs["trainer_email"], kwargs["roster"])
            raise AssertionError(f"expected ValueError for {kwargs}")
        except ValueError:
            pass


def test_validate_create_empty_roster_allowed():
    # Code-only workshops (WS-2): the trainer invites nobody up front and hands
    # out the join code instead, so an empty roster is a valid create.
    for roster in ([], None, ["not-an-email"]):
        assert ls.validate_create("T", "t", TRAINER, roster)["roster"] == []


# ── State transitions ────────────────────────────────────────────────────────

def test_start_open_to_running():
    assert ls.apply_transition("open", "start") == ("running", True)


def test_start_idempotent_when_running():
    assert ls.apply_transition("running", "start") == ("running", False)


def test_start_after_ended_is_illegal():
    try:
        ls.apply_transition("ended", "start")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "ended" in str(exc)


def test_end_from_open_and_running():
    assert ls.apply_transition("open", "end") == ("ended", True)
    assert ls.apply_transition("running", "end") == ("ended", True)


def test_end_idempotent_when_ended():
    assert ls.apply_transition("ended", "end") == ("ended", False)


def test_delete_allowed_before_start_and_after_finish():
    """A finished workshop is a record the trainer may clear. Refusing "ended"
    left every finished room in the trainer's list until the 7-day TTL."""
    for state in ("scheduled", "open", "ended", "cancelled"):
        assert ls.apply_transition(state, "delete") == ("deleted", True)


def test_delete_refused_while_running():
    """The one state that must stay undeletable — it would strand the cohort."""
    try:
        ls.apply_transition("running", "delete")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "running" in str(exc)


def test_unknown_action_and_state_rejected():
    for state, action in (("open", "pause"), ("bogus", "start"), ("", "end")):
        try:
            ls.apply_transition(state, action)
            raise AssertionError(f"expected ValueError for {state}/{action}")
        except ValueError:
            pass


# ── Roster / trainer gating ──────────────────────────────────────────────────

def test_is_trainer_case_insensitive():
    sess = _session()
    assert ls.is_trainer(" Trainer@DYNATRACE.com ", sess)
    assert not ls.is_trainer("learner@x.com", sess)
    assert not ls.is_trainer("", sess)  # empty caller never matches


def test_join_error_not_on_roster():
    assert ls.join_error("open", "stranger@x.com", {"alice@x.com"}) == \
        (403, "email is not on the session roster")


def test_join_error_ended():
    assert ls.join_error("ended", "alice@x.com", {"alice@x.com"}) == \
        (409, "session has ended")


def test_join_allowed_open_and_running():
    assert ls.join_error("open", "Alice@X.com ", {"alice@x.com"}) is None
    assert ls.join_error("running", "alice@x.com", {"alice@x.com"}) is None


def test_is_listed_roster_trainer_and_ended():
    sess = _session(state="open")
    roster = {"alice@x.com"}
    assert ls.is_listed(sess, roster, "alice@x.com")
    assert ls.is_listed(sess, roster, TRAINER)      # trainer sees it too
    assert not ls.is_listed(sess, roster, "other@x.com")
    assert not ls.is_listed(_session(state="ended"), roster, "alice@x.com")
    assert not ls.is_listed({}, roster, "alice@x.com")  # expired hash


def test_is_listed_trainer_rows_scoped_to_owner_tenant():
    # WS-1: a trainer's own workshops are listed only on the tenant they were
    # created from, so the same person on another tenant doesn't see a board
    # they can't drive. Learners are unaffected (next test).
    sess = _session(state="open")
    sess["ownerTenant"] = "https://abc12345.apps.dynatrace.com"
    assert ls.is_listed(sess, set(), TRAINER, "https://abc12345.apps.dynatrace.com")
    assert not ls.is_listed(sess, set(), TRAINER, "https://sro97894.apps.dynatrace.com")
    # Legacy rows (no ownerTenant) and callers that send no tenant still list.
    assert ls.is_listed(_session(state="open"), set(), TRAINER, "https://any.apps.dynatrace.com")
    assert ls.is_listed(sess, set(), TRAINER, "")


def test_is_listed_roster_member_crosses_tenants():
    # The whole point of a cross-tenant workshop: an invited learner sees it
    # from whichever tenant they happen to run.
    sess = _session(state="open")
    sess["ownerTenant"] = "https://abc12345.apps.dynatrace.com"
    assert ls.is_listed(sess, {"alice@x.com"}, "alice@x.com", "https://sro97894.apps.dynatrace.com")


def test_is_listed_co_trainer_crosses_tenants():
    # A co-trainer was named by address, exactly like a roster entry, so the
    # tenant scope must not apply to them: added on COE, signed into sprint,
    # the workshop vanished from their home page and their Workshops list.
    # Only the LEAD stays scoped (that is the case WS-1 was written for).
    sess = _session(state="open", trainers=[TRAINER, "co@dynatrace.com"])
    sess["ownerTenant"] = "https://geu80787.apps.dynatrace.com"
    other = "https://ydi9582h.sprint.apps.dynatracelabs.com"
    assert ls.is_listed(sess, set(), "co@dynatrace.com", other)
    assert ls.is_listed(sess, set(), "co@dynatrace.com", sess["ownerTenant"])
    # The lead is still tenant-scoped, and a stranger is still not a member.
    assert not ls.is_listed(sess, set(), TRAINER, other)
    assert not ls.is_listed(sess, set(), "nobody@x.com", other)


# ── Response shaping ─────────────────────────────────────────────────────────

def test_shape_summary_learner():
    joined = {"alice@x.com": "2026-07-14T10:00:00+00:00"}
    item = ls.shape_summary("sid-1", _session(), {"alice@x.com", "bob@x.com"},
                            joined, "Alice@X.com")
    assert item == {
        "sessionId": "sid-1", "title": "K8s 101 — EMEA Bootcamp",
        "trainingId": "kubernetes-101", "state": "open",
        "trainers": [TRAINER], "trainerEmail": TRAINER,
        "joinedCount": 1, "rosterCount": 2,
        "createdAt": "2026-07-14T09:00:00+00:00", "startedAt": "",
        "isTrainer": False, "isOwner": False, "hasJoined": True,
        # Always present, never inferred from absence (EPIC-007).
        "roomOpen": False, "gateAhead": False,
    }


def test_shape_summary_echoes_when_provisioning_was_requested():
    """The registrants panel shows "requested at" from the LIST row.

    Without it on the summary a trainer had no on-screen record that their
    press landed, and the honest response to "did that work?" was to press
    Start provisioning again. Truthy-only, like every other workshop field:
    a workshop nobody has provisioned adds no key at all.
    """
    quiet = ls.shape_summary("sid-1", _session(), set(), {}, TRAINER)
    assert "provisionRequestedAt" not in quiet

    asked = _session()
    asked["provisionRequestedAt"] = "2026-08-18T08:32:00+00:00"
    # Stored alongside it, deliberately NOT echoed — the app shows a time, and
    # which trainer pressed the button is nobody else's business.
    asked["provisionRequestedBy"] = TRAINER
    item = ls.shape_summary("sid-1", asked, set(), {}, "learner@x.com")
    assert item["provisionRequestedAt"] == "2026-08-18T08:32:00+00:00"
    assert "provisionRequestedBy" not in item


def test_shape_summary_trainer_not_joined():
    item = ls.shape_summary("sid-1", _session(), {"alice@x.com"}, {}, TRAINER)
    assert item["isTrainer"] is True
    assert item["hasJoined"] is False
    assert item["joinedCount"] == 0


def test_shape_detail_learner_gets_counts_only():
    joined = {"alice@x.com": "2026-07-14T10:00:00+00:00"}
    out = ls.shape_detail("sid-1", _session(), {"alice@x.com", "bob@x.com"},
                          joined, "alice@x.com")
    assert out["joinedCount"] == 1
    assert out["rosterCount"] == 2
    assert "roster" not in out
    assert "joined" not in out


def test_shape_detail_trainer_gets_roster_and_joined():
    joined = {"bob@x.com": "2026-07-14T10:05:00+00:00",
              "alice@x.com": "2026-07-14T10:00:00+00:00"}
    out = ls.shape_detail("sid-1", _session(), {"bob@x.com", "alice@x.com"},
                          joined, " Trainer@Dynatrace.com ")
    assert out["roster"] == ["alice@x.com", "bob@x.com"]  # sorted
    assert out["joined"] == [
        {"email": "alice@x.com", "joinedAt": "2026-07-14T10:00:00+00:00", "tenant": ""},
        {"email": "bob@x.com", "joinedAt": "2026-07-14T10:05:00+00:00", "tenant": ""},
    ]


def test_shape_detail_trainer_joined_rows_carry_the_checkin_tenant():
    """The trainer's board shows WHERE each learner will run the workshop —
    the tenant bound at Provision-here (or re-bound by a later check-in)."""
    joined = {"bob@x.com": "2026-07-14T10:05:00+00:00"}
    tenants = {"bob@x.com": "https://abc123.apps.dynatrace.com"}
    out = ls.shape_detail("sid-1", _session(), {"bob@x.com"}, joined,
                          " Trainer@Dynatrace.com ", tenants=tenants)
    assert out["joined"] == [
        {"email": "bob@x.com", "joinedAt": "2026-07-14T10:05:00+00:00",
         "tenant": "https://abc123.apps.dynatrace.com"},
    ]


def test_shape_detail_my_tenant_is_the_callers_own_binding():
    """A learner must be able to tell "I am already provisioning here" from "I
    bound somewhere else" — without seeing anybody else's tenant."""
    joined = {"alice@x.com": "2026-07-14T10:00:00+00:00",
              "bob@x.com": "2026-07-14T10:05:00+00:00"}
    tenants = {"alice@x.com": "https://abc123.apps.dynatrace.com",
               "bob@x.com": "https://zzz999.apps.dynatrace.com"}
    out = ls.shape_detail("sid-1", _session(), set(joined), joined,
                          " Alice@X.com ", tenants={"alice@x.com": tenants["alice@x.com"]})
    assert out["myTenant"] == "https://abc123.apps.dynatrace.com"
    # …and nothing about bob, who is not the caller.
    assert "joined" not in out
    assert "zzz999" not in repr(out)


def test_shape_detail_my_tenant_empty_when_unbound_or_unread():
    joined = {"alice@x.com": "2026-07-14T10:00:00+00:00"}
    # Checked in before tenants were recorded.
    assert ls.shape_detail("sid-1", _session(), set(joined), joined,
                           "alice@x.com", tenants={})["myTenant"] == ""
    # Write-echo callers pass no tenants hash at all.
    assert ls.shape_detail("sid-1", _session(), set(joined), joined,
                           "alice@x.com")["myTenant"] == ""
    # Anonymous caller has nobody to answer it for.
    assert ls.shape_detail("sid-1", _session(), set(joined), joined,
                           "", tenants={"alice@x.com": "https://a.b"})["myTenant"] == ""


def test_shape_detail_includes_all_scalar_fields():
    out = ls.shape_detail("sid-1", _session(state="ended", ref="feat/x"),
                          set(), {}, "learner@x.com")
    for field in ("sessionId", "title", "trainingId", "ref", "state",
                  "trainerEmail", "createdAt", "startedAt", "endedAt",
                  "joinedCount", "rosterCount"):
        assert field in out
    assert out["ref"] == "feat/x"
    assert out["state"] == "ended"


# ── roster_targets (WS-4: trainer runs the lab too) ──────────────────────────

ROSTER = {"bob@x.com", "alice@x.com"}
TRAINER = "trainer@dynatrace.com"


def test_roster_targets_learners_only_by_default():
    assert ls.roster_targets(ROSTER, TRAINER, include_trainer=False) == [
        ("alice@x.com", "learner"), ("bob@x.com", "learner")]


def test_roster_targets_appends_trainer_last_when_asked():
    assert ls.roster_targets(ROSTER, " Trainer@Dynatrace.com ", True) == [
        ("alice@x.com", "learner"), ("bob@x.com", "learner"),
        (TRAINER, "trainer")]


def test_roster_targets_never_duplicates_a_trainer_on_the_roster():
    # A trainer who invited themselves gets ONE row, as a learner — otherwise
    # provision-all would queue two environments for the same person.
    roster = ROSTER | {TRAINER}
    out = ls.roster_targets(roster, TRAINER, include_trainer=True)
    assert [e for e, _ in out].count(TRAINER) == 1
    assert dict(out)[TRAINER] == "learner"


def test_roster_targets_ignores_a_missing_trainer_email():
    assert ls.roster_targets(ROSTER, "", True) == \
        ls.roster_targets(ROSTER, None, True) == \
        ls.roster_targets(ROSTER, TRAINER, False)


def test_roster_targets_works_for_a_code_only_workshop():
    # WS-2 workshops start with an empty roster; the trainer must still get one.
    assert ls.roster_targets(set(), TRAINER, True) == [(TRAINER, "trainer")]
    assert ls.roster_targets(None, TRAINER, True) == [(TRAINER, "trainer")]


def test_only_the_CALLER_is_exempt_from_the_binding_rules():
    """A trainer TEAM spans tenants, so "the trainer is calling from the
    workshop tenant" is a fact about the caller and about nobody else.

    This used to exempt every trainer row, and provision-all then built each
    co-trainer a container on the CALLER's tenant — sprint, for a lead bound to
    COE (ws_msxt044r-bed99c, 2026-08-18). The behavioural half of this rule is
    pinned in test_live_provisioning.py; here we only keep the blanket
    role-based exemptions from coming back.
    """
    import inspect
    from dashboard import app as a
    src = inspect.getsource(a.api_live_session_provision_all)
    assert 'if role == "learner" else ""' not in src, \
        "provision-all must place co-trainers by their own binding, not by role"
    assert "email == caller" in src, \
        "the caller is the one exemption; it has to be spelled out"
    ready = inspect.getsource(a.api_live_session_readiness)
    assert '"none" if role == "trainer"' not in ready, \
        "readiness must classify a co-trainer by their binding, not by role"


def test_chunk_filter_applies_to_learners_only():
    """The app chunks big rosters, so `emails` is one slice of it. If the
    trainer were subject to that filter, includeTrainer would silently no-op on
    every chunk that doesn't happen to contain the trainer's own address."""
    import inspect
    from dashboard import app as a
    src = inspect.getsource(a.api_live_session_provision_all)
    assert "role == live_sessions.LEARNER_ROLE" in src, \
        "the chunk filter must skip learners only"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all live-sessions tests passed")


def test_join_error_allows_trainer_into_own_workshop():
    """The trainer is never on their own roster but must be able to enter.

    Regression: trainer could start a workshop and then get 403 joining it.
    """
    sess = {"trainerEmail": "trainer@dynatrace.com"}
    assert ls.join_error("running", "trainer@dynatrace.com", set(), sess) is None
    assert ls.join_error("open", "trainer@dynatrace.com", {"learner@x.com"}, sess) is None


def test_join_error_still_blocks_strangers():
    """Allowing the trainer must not open the room to everyone."""
    sess = {"trainerEmail": "trainer@dynatrace.com"}
    assert ls.join_error("running", "stranger@x.com", set(), sess) == (
        403, "email is not on the session roster")


def test_join_error_trainer_still_blocked_after_end():
    """Trainer bypass is roster-only — lifecycle rules still apply."""
    sess = {"trainerEmail": "trainer@dynatrace.com"}
    assert ls.join_error("ended", "trainer@dynatrace.com", set(), sess)[0] == 409
    assert ls.join_error("cancelled", "trainer@dynatrace.com", set(), sess)[0] == 409


def test_join_error_without_session_keeps_roster_rule():
    """Callers that pass no session (older call sites) behave exactly as before."""
    assert ls.join_error("running", "someone@x.com", set()) == (
        403, "email is not on the session roster")


# ── Pacing gate: "unlock path with mine" ─────────────────────────────────────

_PACING_DEFAULT = {"trainerStep": 0, "unlockPath": False, "gateAhead": False,
                   "pacingBy": "", "pacingAt": ""}


def test_pacing_state_defaults_to_locked():
    assert ls.pacing_state({}) == _PACING_DEFAULT
    assert ls.pacing_state(None) == _PACING_DEFAULT


def test_pacing_state_reads_stored_strings():
    # Redis hash values are always strings.
    assert ls.pacing_state({"trainerStep": "4", "unlockPath": "1",
                            "gateAhead": "1", "pacingBy": "co@x.com",
                            "pacingAt": "2026-08-06T13:00:00+00:00"}) == {
        "trainerStep": 4, "unlockPath": True, "gateAhead": True,
        "pacingBy": "co@x.com", "pacingAt": "2026-08-06T13:00:00+00:00"}


def test_both_pacing_toggles_are_independent_opt_ins():
    """A trainer who touches nothing gets sequential-own-pace: no solutions
    released, no ceiling on how far a learner may read."""
    untouched = {"trainerStep": "2"}
    assert ls.solution_visible(1, untouched) is False
    assert ls.step_visible(99, untouched) is True


def test_trainer_always_sees_every_solution():
    """Trainers are not always experts on the content — and Run solution is how
    they recover a broken demo in front of a room."""
    sealed = {"trainerStep": "0", "unlockPath": "0"}
    assert ls.solution_visible(1, sealed, is_trainer=True) is True
    assert ls.solution_visible(99, sealed, is_trainer=True) is True


def test_learner_sees_nothing_while_toggle_is_off():
    """A training that should never reveal answers leaves the toggle off."""
    s = {"trainerStep": "9", "unlockPath": "0"}
    assert ls.solution_visible(1, s) is False


def test_learner_sees_steps_UP_TO_AND_INCLUDING_the_trainers():
    """EPIC-007: the ceiling is the trainer's own step, not the one before it.

    A straggler is stuck on the step the class is doing NOW, so that is the step
    whose solution has to be reachable. The old rule (strictly past) released
    everything except the one they actually needed.
    """
    s = {"trainerStep": "4", "unlockPath": "1"}
    assert [ls.solution_visible(n, s) for n in (1, 2, 3, 4)] == [True] * 4
    assert ls.solution_visible(5, s) is False


def test_moving_on_releases_exactly_one_more_step():
    on4 = {"trainerStep": "4", "unlockPath": "1"}
    on5 = {"trainerStep": "5", "unlockPath": "1"}
    assert ls.solution_visible(5, on4) is False
    assert ls.solution_visible(5, on5) is True
    assert ls.solution_visible(6, on5) is False


def test_trainer_on_first_step_releases_that_step():
    s = {"trainerStep": "1", "unlockPath": "1"}
    assert ls.solution_visible(1, s) is True
    assert ls.solution_visible(2, s) is False


def test_unmoved_pointer_releases_nothing():
    """A workshop nobody has paced yet has released no solutions — the pointer
    is NOT floored here (unlike the gate-ahead ceiling)."""
    s = {"trainerStep": "0", "unlockPath": "1"}
    assert ls.solution_visible(1, s) is False


# ── Gate-ahead: hold the learners who race ───────────────────────────────────

def test_gate_ahead_off_lets_a_learner_read_anything():
    assert ls.step_visible(99, {"trainerStep": "2"}) is True


def test_gate_ahead_caps_a_learner_at_the_class_pointer():
    s = {"trainerStep": "3", "gateAhead": "1"}
    assert [ls.step_visible(n, s) for n in (1, 2, 3)] == [True, True, True]
    assert ls.step_visible(4, s) is False


def test_gate_ahead_with_an_unmoved_pointer_still_allows_step_one():
    """trainerStep is 0 until someone moves it. Read literally that locks a
    gated learner out of the whole workshop, so the ceiling floors at step 1."""
    s = {"gateAhead": "1"}
    assert ls.step_visible(1, s) is True
    assert ls.step_visible(2, s) is False


def test_gate_ahead_never_applies_to_a_trainer():
    s = {"trainerStep": "1", "gateAhead": "1"}
    assert ls.step_visible(99, s, is_trainer=True) is True


def test_unparseable_step_is_gated_not_opened():
    """Unlike solution_visible (where garbage reads as 'behind' and is
    harmless), an unreadable step number must not open a gated step."""
    assert ls.step_visible("not-a-number", {"gateAhead": "1"}) is False


def test_unparseable_step_is_treated_as_sealed():
    s = {"trainerStep": "3", "unlockPath": "1"}
    assert ls.solution_visible("not-a-number", s) is True  # -1 < 3
    assert ls.solution_visible(None, s) is True


# ── Past workshops (D1) ──────────────────────────────────────────────────────
#
# Pressing End used to remove the workshop from EVERYONE, trainer included:
# is_listed returned False on state before it ever reached the trainer check.
# The cohort, the scores and the questions all became unreachable, and the
# completion record written at that moment had no surface that could show it.

_ENDED = {"trainerEmail": "trainer@dynatrace.com", "state": "ended",
          "ownerTenant": "https://geu80787.apps.dynatrace.com"}
_ROSTER = {"learner@dynatrace.com"}


def test_ended_workshops_stay_out_of_the_live_listing():
    # The home banner and classroom router treat "listed" as "go here now".
    assert not ls.is_listed(_ENDED, _ROSTER, "trainer@dynatrace.com")
    assert not ls.is_listed(_ENDED, _ROSTER, "learner@dynatrace.com")


def test_the_trainer_can_still_reach_a_finished_workshop():
    assert ls.is_past(_ENDED, _ROSTER, "trainer@dynatrace.com")


def test_a_learner_can_still_reach_a_workshop_they_attended():
    assert ls.is_past(_ENDED, _ROSTER, "learner@dynatrace.com")


def test_a_stranger_cannot():
    assert not ls.is_past(_ENDED, _ROSTER, "someone@else.com")


def test_a_cancelled_workshop_is_also_past_not_listed():
    cancelled = {**_ENDED, "state": "cancelled"}
    assert ls.is_past(cancelled, _ROSTER, "learner@dynatrace.com")
    assert not ls.is_listed(cancelled, _ROSTER, "learner@dynatrace.com")


def test_a_running_workshop_is_not_past():
    for state in ("scheduled", "open", "running"):
        assert not ls.is_past({**_ENDED, "state": state}, _ROSTER, "trainer@dynatrace.com")


def test_past_and_listed_agree_on_who_belongs():
    """The two views must never disagree about membership — that is a disclosure
    bug. Both delegate to is_member for exactly that reason."""
    running = {**_ENDED, "state": "running"}
    for email in ("trainer@dynatrace.com", "learner@dynatrace.com", "someone@else.com"):
        assert ls.is_listed(running, _ROSTER, email) == ls.is_past(_ENDED, _ROSTER, email), email


def test_a_trainer_on_another_tenant_is_still_scoped_out_of_past():
    # WS-1 tenant scoping applies to the trainer side in both views.
    assert not ls.is_past(_ENDED, set(), "trainer@dynatrace.com",
                          tenant="https://sro97894.apps.dynatrace.com")
    assert ls.is_past(_ENDED, set(), "trainer@dynatrace.com",
                      tenant="https://geu80787.apps.dynatrace.com")


def test_finished_workshop_artefacts_outlive_nothing():
    """The session hash must not expire before the results it is listed against.

    At 7 days the frozen record (30 days) outlived the session hash, so a
    three-week-old cohort was unreachable for its remaining 23 days.
    """
    import dashboard.live_pad as lp
    assert ls.SESSION_TTL_SECONDS >= lp.EXPORT_TTL_SECONDS


# ── Provision requests (cross-tenant, PASS 2) ────────────────────────────────

def test_provision_request_is_pending_until_the_learner_is_marked_done():
    """The flag is workshop-level; :provdone is the per-learner half.

    This is what lets a straggler who arrives after the trainer clicked be
    provisioned on arrival with no second click.
    """
    sess = _session(state="running", provisionRequestedAt="2026-08-07T10:00:00+00:00")
    assert ls.provision_request_pending(sess, "late@x.com", {})
    assert ls.provision_request_pending(sess, "late@x.com", {"other@x.com": "queued"})
    assert not ls.provision_request_pending(sess, "late@x.com", {"late@x.com": "queued"})


def test_provision_request_normalizes_the_callers_email():
    """The done-marker is written lowercase; the caller's email is not."""
    sess = _session(state="running", provisionRequestedAt="2026-08-07T10:00:00+00:00")
    assert not ls.provision_request_pending(sess, "  Late@X.com ", {"late@x.com": "queued"})


def test_provision_request_fires_before_start_but_never_after_end():
    """Pre-start provisioning is deliberate: the trainer readies environments
    before the room opens. A finished workshop must still never provision —
    re-opening it days later must not silently build a container for whoever
    loads the page.
    """
    flag = {"provisionRequestedAt": "2026-08-07T10:00:00+00:00"}
    for state in ("scheduled", "open", "running"):
        assert ls.provision_request_pending(_session(state=state, **flag), "a@x.com", {}), state
    for state in ("ended", "cancelled"):
        assert not ls.provision_request_pending(_session(state=state, **flag), "a@x.com", {}), state


def test_no_request_no_provisioning():
    """A workshop nobody asked to provision must never auto-provision."""
    assert not ls.provision_request_pending(_session(state="running"), "a@x.com", {})
    assert not ls.provision_request_pending(
        _session(state="running", provisionRequestedAt=""), "a@x.com", {})


def test_provision_request_ignores_an_empty_caller():
    """An anonymous poll must not match the roster-wide flag."""
    sess = _session(state="running", provisionRequestedAt="2026-08-07T10:00:00+00:00")
    assert not ls.provision_request_pending(sess, "", {})
    assert not ls.provision_request_pending(sess, None, {})


def test_shape_detail_scopes_the_request_to_the_caller():
    """provisionRequested drives an auto-provision, so it must answer 'do I
    need one', never 'does anyone'."""
    sess = _session(state="running", provisionRequestedAt="2026-08-07T10:00:00+00:00")
    done = {"done@x.com": "queued"}
    waiting = ls.shape_detail("ws_1", sess, {"done@x.com", "wait@x.com"},
                              {}, "wait@x.com", done)
    settled = ls.shape_detail("ws_1", sess, {"done@x.com", "wait@x.com"},
                              {}, "done@x.com", done)
    assert waiting["provisionRequested"] is True
    assert settled["provisionRequested"] is False


def test_shape_detail_without_provision_done_never_triggers():
    """start/end/patch echo the detail back after a write. They pass no
    done-map, and must not be read as 'you have a request waiting'."""
    sess = _session(state="running", provisionRequestedAt="2026-08-07T10:00:00+00:00")
    assert ls.shape_detail("ws_1", sess, set(), {}, "a@x.com")["provisionRequested"] is False


SRO_T = "https://sro97894.apps.dynatrace.com"
COE_T = "https://geu80787.apps.dynatrace.com"


def test_readiness_reports_requested_over_foreign():
    """'foreign' tells the trainer where someone sits; 'requested' tells them
    something is still going to happen. The second is the actionable one."""
    assert ls.readiness_gap_state(True, SRO_T, COE_T, requested=True) == "requested"
    assert ls.readiness_gap_state(True, SRO_T, COE_T, requested=False) == "foreign"
    assert ls.readiness_gap_state(True, COE_T, COE_T, requested=True) == "requested"


def test_readiness_never_hides_a_learner_who_never_arrived():
    """not-joined outranks requested. A trainer has to act on an empty seat,
    and 'requested' would make an empty room look like a busy one."""
    assert ls.readiness_gap_state(False, "", COE_T, requested=True) == "not-joined"


def test_readiness_gap_state_default_is_the_old_contract():
    """The new argument is opt-in — legacy callers keep their behaviour."""
    assert ls.readiness_gap_state(True, SRO_T, COE_T) == "foreign"
    assert ls.readiness_gap_state(True, COE_T, COE_T) == "none"
    assert ls.readiness_gap_state(True, SRO_T, "", requested=True) == "none"


# ── Audit trail ──────────────────────────────────────────────────────────────

def test_audit_event_is_flat_strings_for_xadd():
    ev = ls.audit_event(ls.EVENT_PROVISION_ACCEPTED, email="A@X.com",
                        tenant="https://SRO97894-1.apps.dynatrace.com/",
                        actor="trainer@x.com", detail="ok", now="2026-08-07T10:00:00+00:00")
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in ev.items())
    assert ev["email"] == "a@x.com"
    # Same normalization as the :tenants hash, or the audit trail and the
    # provisioning decision would disagree about which tenant someone is in.
    # That shared answer is now an environment ID rather than a URL — the four
    # shapes a tenant arrives in have no common URL form, only a common id.
    assert ev["tenant"] == "sro97894"
    assert ev["kind"] == "provision-accepted"


def test_audit_event_drops_empty_fields():
    """A reader must never have to tell 'absent' from 'empty string'."""
    ev = ls.audit_event(ls.EVENT_STARTED, actor="t@x.com", now="2026-08-07T10:00:00+00:00")
    assert "email" not in ev and "tenant" not in ev and "detail" not in ev
    assert ev["actor"] == "t@x.com"


def test_audit_event_rejects_an_unknown_kind():
    """The kinds are a closed set — the client filters on them."""
    try:
        ls.audit_event("provision-maybe")
    except ValueError:
        return
    raise AssertionError("unknown kind must raise")


def test_audit_event_truncates_detail():
    ev = ls.audit_event(ls.EVENT_PROVISION_FAILED, detail="x" * 5000)
    assert len(ev["detail"]) == 600


def test_audit_event_keeps_the_REASON_of_a_mint_failure_not_just_its_preamble():
    """The cap must clear the boilerplate a mint failure arrives with.

    At 200 the APAC bootcamp recorded this, in the stream a trainer reads
    mid-delivery, with the remediation sentence cut off:

        …platform mint failed: Blocked request to 'sso.dynatrace.com' (host
        not in allowlist). To find out about how to m

    The preamble alone is 113 characters, so the cap has to be generous enough
    that the part naming the CAUSE and the FIX both survive.
    """
    real = ("Could not mint Dynatrace tokens on this tenant, so the training was not "
            "started. platform mint failed: Blocked request to 'sso.dynatrace.com' "
            "(host not in allowlist). To find out about how to allow outbound "
            "connections, see the documentation.")
    ev = ls.audit_event(ls.EVENT_PROVISION_FAILED, detail=real)
    assert ev["detail"] == real, "a real mint failure must survive intact"
    assert "host not in allowlist" in ev["detail"]


def test_shape_events_keeps_the_stream_id_for_paging():
    """The id is what makes the trainer's toast fire once per learner instead
    of once per poll."""
    rows = ls.shape_events([("1754-0", {"kind": "joined", "email": "a@x.com"}),
                            ("1755-0", {"kind": "started", "actor": "t@x.com"})])
    assert [r["id"] for r in rows] == ["1754-0", "1755-0"]
    assert rows[0]["kind"] == "joined"
    assert ls.shape_events(None) == []


def test_events_stream_is_capped():
    """Uncapped XADD grows for the 30-day life of the session keys."""
    assert 0 < ls.EVENTS_MAXLEN <= 10000


def test_class_pointer_of_reads_the_raw_session_hash():
    """Public helper for callers holding the session hash (the provision-time
    resume clamp). Floors at 1 exactly like the gate the client mirrors."""
    assert ls.class_pointer_of({}) == 1
    assert ls.class_pointer_of({"trainerStep": "0"}) == 1
    assert ls.class_pointer_of({"trainerStep": "7"}) == 7


# ── Cross-tenant admin view (Workshops & Delivery tab) ───────────────────────

def test_seat_summary_counts_registrations_not_check_ins():
    """A seat is consumed by REGISTERING — that is what the join-by-code seat
    check enforces — so `present` has to be reported separately."""
    s = ls.seat_summary({"a@x.com", "b@x.com"}, {"a@x.com": "t"}, "10")
    assert s == {"seatsTaken": 2, "maxSeats": 10, "seatsOpen": 8, "present": 1}


def test_seat_summary_unlimited_is_none_not_zero():
    """'Unlimited' and 'full' must not render the same way."""
    assert ls.seat_summary({"a@x.com"}, {}, "")["seatsOpen"] is None
    assert ls.seat_summary({"a@x.com"}, {}, None)["seatsOpen"] is None
    assert ls.seat_summary({"a@x.com"}, {}, "0")["seatsOpen"] is None


def test_seat_summary_never_reports_negative_free_seats():
    # A roster edit can legitimately exceed maxSeats; the dashboard shows 0.
    assert ls.seat_summary({"a@x.com", "b@x.com"}, {}, "1")["seatsOpen"] == 0


def test_seat_summary_tolerates_junk_max_seats():
    assert ls.seat_summary(set(), {}, "not-a-number")["maxSeats"] == 0


def test_schedule_sort_key_prefers_when_it_happens():
    assert ls.schedule_sort_key({"scheduledAt": "2026-09-01T09:00:00+00:00",
                                 "createdAt": "2026-08-01T09:00:00+00:00"}) \
        == "2026-09-01T09:00:00+00:00"


def test_schedule_sort_key_falls_back_to_created():
    assert ls.schedule_sort_key({"createdAt": "2026-08-01T09:00:00+00:00"}) \
        == "2026-08-01T09:00:00+00:00"
    assert ls.schedule_sort_key({}) == ""
    assert ls.schedule_sort_key(None) == ""


def _admin_session():
    return {"title": "Kubernetes 101", "trainingId": "kubernetes-101",
            "state": "scheduled", "trainers": '["lead@x.com", "co@x.com"]',
            "ownerTenant": "https://geu80787.apps.dynatrace.com",
            "createdAt": "2026-08-13T09:00:00+00:00",
            "scheduledAt": "2026-09-01T09:00:00+00:00",
            "maxSeats": "20", "joinCode": "ABC123"}


def test_shape_admin_row_splits_owner_from_co_trainers():
    row = ls.shape_admin_row("ws_1", _admin_session(), {"a@x.com"}, {}, {})
    assert row["owner"] == "lead@x.com"
    assert row["coTrainers"] == ["co@x.com"]
    assert row["trainers"] == ["lead@x.com", "co@x.com"]


def test_shape_admin_row_emits_owner_tenant():
    """The ONLY payload that carries ownerTenant. workshop_fields deliberately
    does not — a learner has no business knowing which tenant created a
    workshop; this route is GitHub-org-member gated."""
    row = ls.shape_admin_row("ws_1", _admin_session(), set(), {}, {})
    assert row["ownerTenant"] == "https://geu80787.apps.dynatrace.com"
    assert "ownerTenant" not in ls.workshop_fields(_admin_session(), "lead@x.com")


def test_shape_admin_row_has_no_caller_identity_fields():
    """This view has no 'me' — isTrainer/hasJoined would be meaningless."""
    row = ls.shape_admin_row("ws_1", _admin_session(), set(), {}, {})
    for absent in ("isTrainer", "hasJoined", "myTenant", "trainerEmail"):
        assert absent not in row


def test_shape_admin_row_sorts_registrants_and_counts_bindings():
    row = ls.shape_admin_row("ws_1", _admin_session(),
                             {"b@x.com", "a@x.com"},
                             {"a@x.com": "t"},
                             {"a@x.com": "https://sro97894.apps.dynatrace.com"})
    assert row["registrants"] == ["a@x.com", "b@x.com"]
    assert row["joinedCount"] == 1
    assert row["boundCount"] == 1


def test_shape_admin_row_editable_mirrors_the_patch_rule():
    """PATCH refuses anything past `open` — the row must say so rather than
    letting the dashboard offer an edit that will 409."""
    for state, editable in (("scheduled", True), ("open", True),
                            ("running", False), ("ended", False),
                            ("cancelled", False)):
        row = ls.shape_admin_row("ws_1", {**_admin_session(), "state": state},
                                 set(), {}, {})
        assert row["editable"] is editable, state


def test_shape_admin_row_survives_a_teamless_session():
    row = ls.shape_admin_row("ws_1", {"title": "x"}, set(), {}, {})
    assert row["owner"] == "" and row["coTrainers"] == []


# ── Tenant canonicalisation ───────────────────────────────────────────────────
# normalize_tenant answers with an environment ID, not a URL, because the same
# tenant reaches it in four shapes and only the id is common to all of them.
# Getting this wrong put a red "wrong tenant" flag on healthy learners and told
# a learner on COE's vanity host that their environment ran somewhere else.

def test_normalize_tenant_reduces_every_shape_to_one_id():
    for raw in ("https://sro97894.apps.dynatrace.com",
                "https://sro97894.apps.dynatrace.com/",
                "HTTPS://SRO97894.apps.dynatrace.com",
                "https://sro97894-1.apps.dynatrace.com",   # app-function runtime
                "sro97894",                                 # bare id
                "sro97894.apps.dynatrace.com"):
        assert ls.normalize_tenant(raw) == "sro97894", raw


def test_normalize_tenant_resolves_the_coe_vanity_alias():
    """COE answers to two names. The browser reports whichever host the person
    clicked; an app-function always reports the canonical one. Grail records
    geu80787, so that is the direction the mapping runs."""
    for raw in ("https://wwse.apps.dynatrace.com", "wwse",
                "https://geu80787.apps.dynatrace.com", "geu80787"):
        assert ls.normalize_tenant(raw) == "geu80787", raw


def test_normalize_tenant_strips_the_app_frame_alias_marker():
    """labSession.getTenantId()'s hostname regex is greedy, so the COE app-frame
    yields `geu80787--alias`. That value is on live jobs today."""
    assert ls.normalize_tenant("geu80787--alias") == "geu80787"


def test_normalize_tenant_leaves_a_managed_tenant_uuid_intact():
    """Only a plain alphanumeric label can carry the runtime's -N suffix."""
    uuid = "abc12345-6789-0abc-def0-123456789abc"
    assert ls.normalize_tenant(f"https://{uuid}.apps.dynatrace.com") == uuid


def test_normalize_tenant_passes_through_empty():
    assert ls.normalize_tenant("") == ""
    assert ls.normalize_tenant(None) == ""


def test_env_tenant_mismatch_ignores_shape_differences():
    """The board's ⚠. Both live attendees tripped it while running perfectly:
    arena_tenant held `geu80787--alias` and `sro97894` while the binding held a
    full URL."""
    assert not ls.env_tenant_mismatch("geu80787--alias",
                                      "https://geu80787.apps.dynatrace.com")
    assert not ls.env_tenant_mismatch("sro97894",
                                      "https://sro97894.apps.dynatrace.com")
    assert not ls.env_tenant_mismatch("https://wwse.apps.dynatrace.com",
                                      "https://geu80787.apps.dynatrace.com")
    # A real mismatch must still be reported.
    assert ls.env_tenant_mismatch("https://ydi9582h.sprint.apps.dynatracelabs.com",
                                  "https://geu80787.apps.dynatrace.com")
