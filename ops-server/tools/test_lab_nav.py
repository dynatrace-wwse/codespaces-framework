#!/usr/bin/env python3
"""Tests for tools/lab_nav.py — the mkdocs nav enumerator.

The whole point of this module is that step ordinals must agree with the
Enablement App, which numbers steps from `mkdocs.yaml`'s `nav:`. Every test here
pins a way that agreement was, or could be, broken.

Run:  python3 -m pytest tools/test_lab_nav.py
  or: python3 tools/test_lab_nav.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lab_nav import build_training_groups, nav_entries, page_is_exempt, parse_nav  # noqa: E402


SIMPLE = """
site_name: Kubernetes 101
nav:
  - "Welcome": index.md
  - "Prerequisites": prerequisites.md
  - "1. Deploy Operator": 1-deploy-operator.md
extra:
  foo: bar
"""

FLOW = """
nav:
  - "Welcome": index.md
  - "1. Bug: Clear Completed": [{'Reproduce': '1-repro.md'}, {'Hunt': '1-hunt.md'}]
  - "Cleanup": cleanup.md
"""

BLOCK_SECTIONS = """
nav:
  - "Platform Best Practices":
      - "Introduction": pbp/00-intro.md
      - "Overview": pbp/01-overview.md
  - "Infrastructure Monitoring":
      - "Introduction": im/00-intro.md
"""

LEXICAL_TRAP = """
nav:
  - "1. About": index.md
  - "2. Prerequisites": 2-getting-started.md
  - "3. Deploy Dynatrace": 4-deploy-dynatrace.md
"""


def _titles(entries):
    return [(e.index, e.filename) for e in entries]


def test_simple_nav_in_order():
    groups = build_training_groups(SIMPLE)
    assert len(groups) == 1
    assert _titles(groups[0].entries) == [
        (0, "index.md"), (1, "prerequisites.md"), (2, "1-deploy-operator.md"),
    ]


def test_nav_order_is_not_filename_order():
    """The regression this module exists for.

    Filename sort puts `index.md` last (after `2-`, `4-`); the learner sees it
    first. Replaying "the first two steps" against a filename-sorted list would
    run the wrong two.
    """
    entries = build_training_groups(LEXICAL_TRAP)[0].entries
    assert entries[0].filename == "index.md"
    assert sorted(e.filename for e in entries)[0] != "index.md"


def test_title_containing_a_colon_is_not_split():
    """"1. Bug: Clear Completed" must not parse as title="1. Bug"."""
    groups = build_training_groups(FLOW)
    entries = groups[0].entries
    assert [e.filename for e in entries] == ["index.md", "1-repro.md", "1-hunt.md", "cleanup.md"]


def test_flow_sections_flatten_into_steps():
    """A nested nav group is several steps to the app, not one."""
    entries = build_training_groups(FLOW)[0].entries
    assert len(entries) == 4
    assert entries[1].title == "Reproduce"
    assert entries[2].title == "Hunt"


def test_all_pure_sections_split_into_trainings():
    """A packed repo (every top-level item is a section) is many trainings."""
    groups = build_training_groups(BLOCK_SECTIONS)
    assert [g.key for g in groups] == ["platform-best-practices", "infrastructure-monitoring"]
    assert len(groups[0].entries) == 2
    # Ordinals restart per training — they are per-training step numbers.
    assert groups[1].entries[0].index == 0


def test_missing_pages_keep_their_ordinal():
    """A nav entry whose file is absent must not shift later steps.

    The importer substitutes placeholder content rather than dropping the step,
    so dropping it here would silently misalign every subsequent ordinal.
    """
    entries = build_training_groups(SIMPLE)[0].entries
    assert entries[2].index == 2


def test_no_nav_key_raises():
    try:
        parse_nav("site_name: x\n")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a config with no nav")


def test_exemption_marker_and_reason():
    exempt, reason = page_is_exempt("# Prereqs\n<!-- LAB_NO_SOLUTION: sanity probe only -->\n")
    assert exempt is True
    assert reason == "sanity probe only"
    assert page_is_exempt("# Prereqs\n")[0] is False


def test_nav_entries_falls_back_to_filename_sort(tmpdir="/tmp"):
    """Repos predating the nav contract must still enumerate."""
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        docs = os.path.join(root, "docs")
        os.makedirs(docs)
        for name in ("b.md", "a.md"):
            open(os.path.join(docs, name), "w").close()
        entries = nav_entries(docs)
        assert [e.filename for e in entries] == ["a.md", "b.md"]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
