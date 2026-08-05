"""Training-id resolution across the two namespaces (arena_training_for_id).

A training carries two names — the Arena catalog id ("kubernetes-101") and the
GitHub repo name ("enablement-kubernetes-101"). Content is addressed by repo,
so the classroom URL carries the repo name; a learner on a tenant whose catalog
cannot translate it back sends the repo name to /api/arena/provision. Strict
id-equality there is what produced

    404 {"detail": "Training 'enablement-kubernetes-101' not found"}

for a cross-tenant workshop learner. These tests pin the resolver's contract and
the invariant that keeps the shared lookup space unambiguous.

No Redis, no HTTP — the resolver is pure over a catalog list.

Runnable two ways:
  - pytest:     /home/ops/ops-venv/bin/python -m pytest dashboard/test_arena_training_id.py
  - standalone: /home/ops/ops-venv/bin/python -m dashboard.test_arena_training_id
"""

import dashboard.app as a

CATALOG = [
    {"id": "kubernetes-101",
     "repoUrl": "https://github.com/dynatrace-wwse/enablement-kubernetes-101"},
    {"id": "log-ingest-101",
     "repoUrl": "https://github.com/dynatrace-wwse/enablement-dynatrace-log-ingest-101"},
    # A training whose repo name does not reduce to its id by prefix-stripping —
    # the shape that makes a "strip the prefix" fix insufficient.
    {"id": "opentelemetry-demo",
     "repoUrl": "https://github.com/dynatrace-wwse/demo-opentelemetry"},
]


# ── Both namespaces resolve ──────────────────────────────────────────────────

def test_catalog_id_resolves():
    assert a.arena_training_for_id(CATALOG, "kubernetes-101")["id"] == "kubernetes-101"


def test_repo_name_resolves_to_the_same_entry():
    # The reported bug, directly.
    by_repo = a.arena_training_for_id(CATALOG, "enablement-kubernetes-101")
    assert by_repo is a.arena_training_for_id(CATALOG, "kubernetes-101")


def test_repo_name_resolves_when_it_does_not_reduce_to_the_id():
    # demo-opentelemetry -> opentelemetry-demo: no prefix-strip gets you there.
    assert a.arena_training_for_id(CATALOG, "demo-opentelemetry")["id"] == "opentelemetry-demo"


def test_matching_is_case_and_whitespace_insensitive():
    assert a.arena_training_for_id(CATALOG, "  Enablement-Kubernetes-101 ")["id"] \
        == "kubernetes-101"


# ── Precedence and rejection ─────────────────────────────────────────────────

def test_catalog_id_wins_over_a_repo_tail_that_shadows_it():
    # Not in the real table today, and _assert_arena_ids_unambiguous forbids it
    # being added — but the resolver must still be deterministic if it ever is.
    shadowed = [
        {"id": "other-training",
         "repoUrl": "https://github.com/dynatrace-wwse/kubernetes-101"},
        *CATALOG,
    ]
    assert a.arena_training_for_id(shadowed, "kubernetes-101")["id"] == "kubernetes-101"


def test_unknown_returns_none():
    assert a.arena_training_for_id(CATALOG, "no-such-training") is None


def test_empty_and_missing_return_none():
    # An empty id must not match the first entry with an empty repoUrl tail.
    assert a.arena_training_for_id(CATALOG, "") is None
    assert a.arena_training_for_id(CATALOG, None) is None
    assert a.arena_training_for_id([{"id": "", "repoUrl": ""}], "") is None


# ── The invariant that keeps the shared lookup space safe ────────────────────

def test_real_catalog_table_is_unambiguous():
    a._assert_arena_ids_unambiguous(a._ARENA_REPOS)


def test_duplicate_ids_are_rejected():
    try:
        a._assert_arena_ids_unambiguous({
            "repo-a": {"id": "same"},
            "repo-b": {"id": "same"},
        })
    except AssertionError as exc:
        assert "duplicate catalog ids" in str(exc)
    else:
        raise AssertionError("duplicate ids should not be accepted")


def test_an_id_equal_to_another_entrys_repo_name_is_rejected():
    try:
        a._assert_arena_ids_unambiguous({
            "kubernetes-101": {"id": "something-else"},
            "enablement-kubernetes-101": {"id": "kubernetes-101"},
        })
    except AssertionError as exc:
        assert "is also the repo name of" in str(exc)
    else:
        raise AssertionError("a cross-namespace collision should not be accepted")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print("all arena training-id tests passed")
