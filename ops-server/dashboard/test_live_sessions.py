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


def _session(state="open", trainer=TRAINER, **extra) -> dict:
    """A live:session:{id} hash as stored in Redis (all-string values)."""
    sess = {
        "title": "K8s 101 — EMEA Bootcamp", "trainingId": "kubernetes-101",
        "ref": "", "trainerEmail": trainer, "state": state,
        "createdAt": "2026-07-14T09:00:00+00:00", "startedAt": "", "endedAt": "",
    }
    sess.update(extra)
    return sess


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


# ── Response shaping ─────────────────────────────────────────────────────────

def test_shape_summary_learner():
    joined = {"alice@x.com": "2026-07-14T10:00:00+00:00"}
    item = ls.shape_summary("sid-1", _session(), {"alice@x.com", "bob@x.com"},
                            joined, "Alice@X.com")
    assert item == {
        "sessionId": "sid-1", "title": "K8s 101 — EMEA Bootcamp",
        "trainingId": "kubernetes-101", "state": "open",
        "trainerEmail": TRAINER, "joinedCount": 1, "rosterCount": 2,
        "createdAt": "2026-07-14T09:00:00+00:00", "startedAt": "",
        "isTrainer": False, "hasJoined": True,
    }


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
        {"email": "alice@x.com", "joinedAt": "2026-07-14T10:00:00+00:00"},
        {"email": "bob@x.com", "joinedAt": "2026-07-14T10:05:00+00:00"},
    ]


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


def test_trainer_is_exempt_from_the_learner_skips():
    """The trainer calls provision-all FROM the workshop tenant and never joins
    their own workshop, so neither skip may ever apply to them — the endpoint
    enforces this by only consulting provision_skip_status for learners."""
    import inspect
    from dashboard import app as a
    src = inspect.getsource(a.api_live_session_provision_all)
    assert 'if role == "learner" else ""' in src, \
        "provision-all must not run the joined/tenant skips against the trainer"
    ready = inspect.getsource(a.api_live_session_readiness)
    assert '"none" if role == "trainer"' in ready, \
        "readiness must not report the trainer as not-joined/foreign"


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

def test_pacing_state_defaults_to_locked():
    assert ls.pacing_state({}) == {"trainerStep": 0, "unlockPath": False}
    assert ls.pacing_state(None) == {"trainerStep": 0, "unlockPath": False}


def test_pacing_state_reads_stored_strings():
    # Redis hash values are always strings.
    assert ls.pacing_state({"trainerStep": "4", "unlockPath": "1"}) == {
        "trainerStep": 4, "unlockPath": True}


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


def test_learner_sees_only_steps_the_trainer_moved_PAST():
    # Trainer on 4 → 1,2,3 released; 4 still sealed (they are teaching it now).
    s = {"trainerStep": "4", "unlockPath": "1"}
    assert [ls.solution_visible(n, s) for n in (1, 2, 3)] == [True, True, True]
    assert ls.solution_visible(4, s) is False
    assert ls.solution_visible(5, s) is False


def test_moving_on_releases_exactly_one_more_step():
    on4 = {"trainerStep": "4", "unlockPath": "1"}
    on5 = {"trainerStep": "5", "unlockPath": "1"}
    assert ls.solution_visible(4, on4) is False
    assert ls.solution_visible(4, on5) is True
    assert ls.solution_visible(5, on5) is False


def test_trainer_on_first_step_releases_nothing():
    s = {"trainerStep": "1", "unlockPath": "1"}
    assert ls.solution_visible(1, s) is False


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
