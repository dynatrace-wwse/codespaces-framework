#!/usr/bin/env python3
"""Tests for tools/training_test_runner.py.

Two layers, both offline:
  1. Pure-logic units: doc-block extraction, quiz validation, doc-dump splitting.
  2. A full end-to-end run against a stub Orbital HTTP server (provision →
     ready-poll → docs → setup/baseline/solve/verify/post → terminate),
     asserting the SUCCESS and FAILURE verdicts and that terminate always fires.

Run:  python3 -m pytest tools/test_training_test_runner.py
  or: python3 tools/test_training_test_runner.py
"""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from training_test_runner import (  # noqa: E402
    FILE_MARK,
    extract_from_docs,
    split_doc_dump,
    validate_question,
)

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "training_test_runner.py")

DOC_GOOD = """# Section 1
<!-- STEP_SETUP
commands:
  - deployTodoApp
-->
Some text.
<!-- LAB_QUESTION
type: shell-verification
question: Is the operator running?
buttonText: Check operator
command: checkOperatorReady
expect:
  operator: exit-zero
-->
<!-- LAB_QUESTION
type: multiple-choice
question: What does the operator manage?
options:
  - DynaKubes
  - Dashboards
correct: 0
-->
<!-- LAB_SOLUTION
commands:
  - solve_step1
verify:
  - is_step1_solved
reveal: |
  Run helm install.
-->
"""

DOC_BROKEN_QUIZ = """# Section 2
<!-- LAB_QUESTION
type: multiple-choice
question: Broken one
options:
  - only-one-option
correct: 5
-->
<!-- LAB_QUESTION
type: dql-verification
question: Missing dql field
buttonText: Run query
-->
<!-- LAB_QUESTION
type: shell-verification
question: gte is not an importer operator
buttonText: Check
command: countPods
expect:
  operator: gte
  value: 2
-->
"""


# ── pure-logic units ────────────────────────────────────────────────────────

def test_extract_from_docs_good():
    setups, solutions, checks, quizzes = extract_from_docs([("01.md", DOC_GOOD)])
    assert setups == [("01.md", "deployTodoApp")]
    assert solutions == [("01.md", ["solve_step1"], ["is_step1_solved"])]
    assert len(checks) == 1
    assert checks[0][2] == "checkOperatorReady"
    assert checks[0][3] == {"operator": "exit-zero"}
    assert [q[1] for q in quizzes] == ["shell-verification", "multiple-choice"]
    assert all(problem == "" for *_x, problem in quizzes)


def test_extract_flags_broken_quizzes():
    _s, _sol, checks, quizzes = extract_from_docs([("02.md", DOC_BROKEN_QUIZ)])
    problems = [problem for *_x, problem in quizzes]
    assert len(quizzes) == 3
    assert all(problems), f"every block should be flagged: {problems}"
    # A shell check the importer would drop must NOT be executed as a check.
    assert checks == []


def test_validate_question_rules():
    assert validate_question({"type": "multiple-choice", "question": "q",
                              "options": ["a", "b"], "correct": 1}) == ""
    assert "out of range" in validate_question(
        {"type": "multiple-choice", "question": "q", "options": ["a", "b"], "correct": 2})
    assert "missing 'dql'" in validate_question(
        {"type": "dql-verification", "question": "q", "buttonText": "b"})
    assert validate_question({"type": "instructor-code", "question": "q"}) == ""
    assert "unknown question type" in validate_question({"type": "nope", "question": "q"})
    assert "exit-zero|contains|not-empty|gt" in validate_question(
        {"type": "shell-verification", "command": "c", "expect": {"operator": "gte", "value": "1"}})


def test_split_doc_dump():
    dump = (f"{FILE_MARK}docs/01-intro.md\n# One\nbody\n"
            f"{FILE_MARK}docs/02-lab.md\n# Two\n")
    files = split_doc_dump(dump)
    assert [f for f, _ in files] == ["01-intro.md", "02-lab.md"]
    assert "# One" in files[0][1] and "# Two" in files[1][1]


# ── stub-Orbital end-to-end ─────────────────────────────────────────────────

class StubOrbital(BaseHTTPRequestHandler):
    """Minimal arena API. Class attrs configure per-test behaviour."""
    solved = False           # flipped by the solve command
    check_passes_baseline = False
    check_ever_passes = True
    status_calls = 0
    log_requests: list = []

    def _json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):  # silence
        pass

    def do_GET(self):
        cls = type(self)
        cls.log_requests.append(("GET", self.path))
        if self.path == "/api/arena/trainings":
            self._json([{
                "id": "k8s-101", "title": "Kubernetes 101", "branch": "main",
                "repoUrl": "https://github.com/dynatrace-wwse/enablement-kubernetes-101",
            }])
        elif self.path.startswith("/api/arena/sessions/") and "/exec-status/" in self.path:
            self._json({"done": True, "stdout": "solved", "stderr": "", "exitCode": 0})
        elif self.path.startswith("/api/arena/sessions/"):
            cls.status_calls += 1
            # first poll: provisioning, afterwards: ready
            self._json({"jobId": "enablement-stub", "status":
                        "provisioning" if cls.status_calls < 2 else "ready"})
        else:
            self._json({"detail": "not found"}, 404)

    def do_POST(self):
        cls = type(self)
        cls.log_requests.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if self.path == "/api/arena/provision":
            self._json({"jobId": "enablement-stub", "status": "provisioning",
                        "tokenProvisioned": True, "dtSessionId": "tt-20260730",
                        "expiresAt": "2026-07-30T23:59:59+00:00"})
        elif self.path.endswith("/exec-start"):
            cls.solved = True
            self._json({"execId": "e1", "done": False})
        elif self.path.endswith("/exec"):
            cmd = body.get("command", "")
            if FILE_MARK.split(":")[0] in cmd and "docs/*.md" in cmd:
                dump = (f"{FILE_MARK}docs/01-lab.md\n" + DOC_GOOD)
                self._json({"stdout": dump, "stderr": "", "exitCode": 0})
            elif "checkOperatorReady" in cmd:
                if cls.check_ever_passes and (cls.solved or cls.check_passes_baseline):
                    self._json({"stdout": "Operator Running", "stderr": "", "exitCode": 0})
                else:
                    self._json({"stdout": "not running", "stderr": "", "exitCode": 1})
            else:  # setup commands, is_step1_solved verify, …
                self._json({"stdout": "ok", "stderr": "", "exitCode": 0})
        elif self.path.endswith("/terminate"):
            self._json({"status": "terminating"})
        else:
            self._json({"detail": "not found"}, 404)


def _run_against_stub(check_ever_passes=True):
    StubOrbital.solved = False
    StubOrbital.check_passes_baseline = False
    StubOrbital.check_ever_passes = check_ever_passes
    StubOrbital.status_calls = 0
    StubOrbital.log_requests = []
    srv = HTTPServer(("127.0.0.1", 0), StubOrbital)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env.pop("TRAINING_TEST_TENANT_URL", None)
        env.update({
            "TRAINING_TEST_READY_POLL_S": "0",
            "TRAINING_TEST_SOLVE_POLL_S": "0",
            "TRAINING_TEST_READY_TIMEOUT_S": "10",
        })
        proc = subprocess.run(
            [sys.executable, RUNNER,
             "--repo", "dynatrace-wwse/enablement-kubernetes-101",
             "--ref", "main",
             "--orbital", f"http://127.0.0.1:{srv.server_address[1]}"],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=5)
    return proc


def test_e2e_success_against_stub():
    proc = _run_against_stub(check_ever_passes=True)
    out = proc.stdout
    assert "TRAINING_TEST: SUCCESS" in out, out + proc.stderr
    assert proc.returncode == 0
    # The solve ran through the app's execLong path…
    assert any(p.endswith("/exec-start") for _m, p in StubOrbital.log_requests)
    # …and the session was always released.
    assert any(p.endswith("/terminate") for _m, p in StubOrbital.log_requests)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text):
    """Strip ANSI colour so assertions test the words, not the escape codes.

    The runner colours its output (PASS green, FAIL red), which splits literals
    like "[check] FAIL" with escape sequences mid-string.
    """
    return _ANSI_RE.sub("", text)


def test_e2e_failure_still_terminates():
    proc = _run_against_stub(check_ever_passes=False)
    out = _plain(proc.stdout)
    assert "TRAINING_TEST: FAILURE" in out, out + proc.stderr
    assert proc.returncode == 1
    # Section-ordered engine: the failing check is reported inside its section
    # and the section verdict line names it.
    assert "[check] FAIL" in out, out
    assert "-> SECTION FAIL" in out, out
    assert any(p.endswith("/terminate") for _m, p in StubOrbital.log_requests)




def test_parse_block_scalars_and_questionaire_exclusion():
    """Regression: dql block scalars must be captured (were dropped -> false
    'missing dql'), and LAB_QUESTIONAIRE must not match as LAB_QUESTION."""
    from app_layer_driver import BLOCK_RE, parse_block
    md = (
        "<!-- LAB_QUESTION\n"
        "type: dql-verification\n"
        "question: \"q\"\n"
        "buttonText: \"Check\"\n"
        "dql: |\n"
        "  fetch logs\n"
        "  | filter contains(content, \"X\")\n"
        "expect:\n"
        "  operator: not-empty\n"
        "-->\n"
        "<!-- LAB_QUESTIONAIRE: k8s-101-fundamentals retake=false -->\n"
    )
    blocks = BLOCK_RE.findall(md)
    assert len(blocks) == 1, blocks
    doc = parse_block(blocks[0][1])
    assert doc.get("dql", "").startswith("fetch logs"), doc
    assert doc.get("expect", {}).get("operator") == "not-empty"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
