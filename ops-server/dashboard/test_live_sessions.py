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
