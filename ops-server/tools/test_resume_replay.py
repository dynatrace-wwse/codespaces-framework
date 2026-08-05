#!/usr/bin/env python3
"""Tests for tools/resume_replay.py — rebuilding a learner's environment.

The two properties worth pinning are the ones that are easy to break silently:
the *prefix* replayed must be the nav prefix (not a filename-sorted one), and
the learner-visible stream must never carry a solution command.

Run:  python3 -m pytest tools/test_resume_replay.py
  or: python3 tools/test_resume_replay.py
"""
import io
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import resume_replay  # noqa: E402

MKDOCS = """site_name: Test
nav:
  - "1. About": index.md
  - "2. Prereqs": 2-prereqs.md
  - "3. Deploy": 4-deploy.md
  - "4. Analyze": 5-analyze.md
"""

PAGE_WITH_SOLUTION = """# Deploy

<!-- LAB_SOLUTION
commands:
  - echo SECRETCOMMAND_ONE
verify:
  - true
-->
"""

PAGE_TWO_SOLUTIONS = """# Deploy

<!-- LAB_SOLUTION
commands:
  - echo SECRETCOMMAND_ONE
-->

More text.

<!-- LAB_SOLUTION
commands:
  - echo SECRETCOMMAND_TWO
-->
"""

PAGE_EXEMPT = """<!-- LAB_NO_SOLUTION: provisioning sanity checks only -->
# Prereqs
"""

PAGE_FAILING = """# Deploy

<!-- LAB_SOLUTION
commands:
  - exit 3
-->
"""


def _repo(pages):
    root = tempfile.mkdtemp()
    docs = os.path.join(root, "docs")
    os.makedirs(docs)
    with open(os.path.join(root, "mkdocs.yaml"), "w") as fh:
        fh.write(MKDOCS)
    for name, body in pages.items():
        with open(os.path.join(docs, name), "w") as fh:
            fh.write(body)
    return docs


def _run(docs, until):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        summary = resume_replay.replay(docs, until)
    return summary, out.getvalue(), err.getvalue()


def test_replays_only_the_requested_prefix():
    docs = _repo({
        "index.md": "# About",
        "2-prereqs.md": PAGE_EXEMPT,
        "4-deploy.md": PAGE_WITH_SOLUTION,
        "5-analyze.md": PAGE_WITH_SOLUTION,
    })
    summary, _out, _err = _run(docs, 3)
    assert len(summary["sections"]) == 3
    assert [s["file"] for s in summary["sections"]] == ["index.md", "2-prereqs.md", "4-deploy.md"]
    assert summary["status"] == "ok"


def test_prefix_follows_nav_not_filename_sort():
    """`index.md` is nav-first but filename-last; --until 1 must replay it."""
    docs = _repo({
        "index.md": "# About",
        "2-prereqs.md": PAGE_EXEMPT,
        "4-deploy.md": PAGE_WITH_SOLUTION,
        "5-analyze.md": PAGE_WITH_SOLUTION,
    })
    summary, _out, _err = _run(docs, 1)
    assert [s["file"] for s in summary["sections"]] == ["index.md"]


def test_solution_commands_never_reach_the_learner_stream():
    """The replayed commands are the lab's answers — stdout is learner-visible."""
    docs = _repo({
        "index.md": "# About",
        "2-prereqs.md": PAGE_EXEMPT,
        "4-deploy.md": PAGE_WITH_SOLUTION,
        "5-analyze.md": "# Analyze",
    })
    _summary, out, err = _run(docs, 3)
    assert "SECRETCOMMAND_ONE" not in out, "solution text leaked into the learner stream"
    assert "SECRETCOMMAND_ONE" in err, "operator transcript should carry the detail"
    assert "section 3 of 3" in out


def test_all_solution_blocks_on_a_page_run():
    """The importer's UI panel shows the first block; the environment needs all."""
    docs = _repo({
        "index.md": "# About",
        "2-prereqs.md": PAGE_EXEMPT,
        "4-deploy.md": PAGE_TWO_SOLUTIONS,
        "5-analyze.md": "# Analyze",
    })
    _summary, _out, err = _run(docs, 3)
    assert "SECRETCOMMAND_ONE" in err
    assert "SECRETCOMMAND_TWO" in err


def test_exempt_page_is_not_a_failure():
    docs = _repo({
        "index.md": "# About",
        "2-prereqs.md": PAGE_EXEMPT,
        "4-deploy.md": PAGE_WITH_SOLUTION,
        "5-analyze.md": "# Analyze",
    })
    summary, _out, _err = _run(docs, 2)
    states = {s["file"]: s["state"] for s in summary["sections"]}
    assert states["2-prereqs.md"] == "exempt"
    assert summary["status"] == "ok"
    assert summary["unrestored"] == []


def test_failing_solution_yields_partial_not_abort():
    """A broken section must not cost the learner the whole environment."""
    docs = _repo({
        "index.md": "# About",
        "2-prereqs.md": PAGE_EXEMPT,
        "4-deploy.md": PAGE_WITH_SOLUTION,
        "5-analyze.md": PAGE_FAILING,
    })
    summary, out, _err = _run(docs, 4)
    assert summary["status"] == "partial"
    assert summary["unrestored"] == ["4. Analyze"]
    assert "could not be fully restored" in out


def test_every_section_failing_is_reported_as_failed():
    docs = _repo({
        "index.md": "# About",
        "2-prereqs.md": PAGE_FAILING,
        "4-deploy.md": PAGE_FAILING,
        "5-analyze.md": "# Analyze",
    })
    summary, _out, _err = _run(docs, 3)
    assert summary["status"] == "failed"


def test_zero_steps_is_a_cold_start_not_an_error():
    docs = _repo({"index.md": "# About"})
    summary, _out, _err = _run(docs, 0)
    assert summary["sections"] == []
    assert summary["status"] == "ok"


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
