#!/usr/bin/env python3
"""resume_replay.py — rebuild a learner's environment to the step they left off at.

Runs INSIDE a freshly provisioned lab container (the `dt` container), after
post-create/post-start and *before* the environment is announced ready. Given
"the learner had completed N steps", it replays the `LAB_SOLUTION` commands of
nav steps 0..N-1 in order, which is exactly what the nightly `training-test`
already does for the whole training — so the same blocks that prove a training
works are what make it resumable.

Two properties this file exists to guarantee:

1. **Ordinals agree with the app.** Steps come from `lab_nav`, i.e. the mkdocs
   `nav:` order the Enablement App numbers steps by — not filename sort.

2. **Nothing leaks.** Solution command text and its output are the answer to the
   lab, gated in the UI behind `canSeeSolutions`. stdout carries only sanitized
   progress ("section 3 of 5: Deploy the DynaKube"); every command, stdout and
   stderr goes to *this process's* stderr, which the caller routes to an
   operator-only sink. Never cross the streams.

Replay is best-effort by design: a failed or slow section is recorded as
unrestored and the run continues, because a partially restored environment the
learner is told about beats no environment at all.

Usage:
  resume_replay.py --docs <dir> --until <N> [--training <key>] [--summary-json <path>]
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app_layer_driver import BLOCK_RE, parse_block  # noqa: E402
from lab_nav import nav_entries, page_is_exempt  # noqa: E402

# Per-section and whole-run ceilings. A section that blows its budget is marked
# unrestored rather than killing the provision — the learner still gets a box.
STEP_TIMEOUT_S = int(os.environ.get("RESUME_STEP_TIMEOUT_S", "300"))
TOTAL_TIMEOUT_S = int(os.environ.get("RESUME_TOTAL_TIMEOUT_S", "900"))

# Same shell contract as app_layer_driver / training_test_runner: solutions run
# in a login-equivalent shell with the framework sourced, and with LAB_WAIT=1 so
# `waitFor*` helpers block for automation instead of answering instantly.
# Sourced, not chained with `&&`: if the framework file is missing the solution
# should fail with its own "command not found" — which names the real problem —
# rather than every section reporting a bare non-zero from the source itself.
_SOURCE = (
    "if [ -f .devcontainer/util/source_framework.sh ]; then "
    "source .devcontainer/util/source_framework.sh >/dev/null 2>&1 || true; fi; "
)
_WAIT = "export LAB_WAIT=1; "


def detail(msg):
    """Operator-only stream. Command text and output MUST only ever go here."""
    print(msg, file=sys.stderr, flush=True)


def learner(msg):
    """Learner-visible stream. Section titles and pass/fail only — no commands."""
    print(msg, flush=True)


def run(cmd, cwd, timeout_s):
    """Run one solution/verify command. Returns (stdout, stderr, rc, timed_out)."""
    full = _WAIT + _SOURCE + cmd
    try:
        p = subprocess.run(
            ["bash", "-c", full],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_s,
        )
        return p.stdout, p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as exc:
        return (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""), "", -1, True


def solutions_for(path):
    """Every LAB_SOLUTION block on a page, in document order.

    Deliberately *all* blocks, not just the first: the app importer's regex is
    non-global so its UI panel shows only the first block on a page, but the
    environment needs every one of them to actually reach that step's end state
    (log-ingest's "Deploy Dynatrace" page carries two — operator, then DynaKubes).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None, []
    blocks = []
    for kind, body in BLOCK_RE.findall(text):
        if kind != "LAB_SOLUTION":
            continue
        try:
            doc = parse_block(body)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        cmds = doc.get("commands") or []
        ver = doc.get("verify") or []
        if cmds or ver:
            blocks.append((list(cmds), list(ver)))
    return text, blocks


def replay(docs_dir, until, training_key="", started=None):
    repo_root = os.path.dirname(os.path.abspath(docs_dir.rstrip("/")))
    entries = nav_entries(docs_dir, training_key)
    target = entries[: max(0, until)]
    started = started if started is not None else time.time()

    sections = []
    total = len(target)
    learner("=== Restoring your progress — %d section%s ===" % (total, "" if total == 1 else "s"))

    for pos, entry in enumerate(target, start=1):
        label = "section %d of %d: %s" % (pos, total, entry.title)
        path = os.path.join(docs_dir, entry.filename)
        text, blocks = solutions_for(path)
        record = {"index": entry.index, "title": entry.title, "file": entry.filename}

        if text is None:
            record["state"] = "missing"
            sections.append(record)
            learner("--- %s — page not found, skipped" % label)
            detail("[skip] %s: %s not readable" % (label, path))
            continue

        exempt, reason = page_is_exempt(text)
        if not blocks:
            record["state"] = "exempt" if exempt else "no-solution"
            record["reason"] = reason
            sections.append(record)
            learner(
                "--- %s — nothing to restore%s" % (label, " (informational section)" if exempt else "")
            )
            detail("[skip] %s: %d solution blocks, exempt=%s %s" % (label, 0, exempt, reason))
            continue

        if time.time() - started > TOTAL_TIMEOUT_S:
            record["state"] = "skipped-budget"
            sections.append(record)
            learner("--- %s — skipped, restore time budget reached" % label)
            detail("[budget] %s skipped after %.0fs" % (label, time.time() - started))
            continue

        learner("=== Restoring %s ===" % label)
        ok = True
        deadline = time.time() + STEP_TIMEOUT_S
        for cmds, ver in blocks:
            for c in cmds:
                budget = int(max(5, deadline - time.time()))
                out, err, rc, timed_out = run(c, repo_root, budget)
                detail("[solve %s] exit=%s%s: %s" % (entry.filename, rc, " TIMEOUT" if timed_out else "", c))
                for ln in (out or "").strip().splitlines()[-40:]:
                    detail("        | %s" % ln)
                if (err or "").strip():
                    for ln in err.strip().splitlines()[-10:]:
                        detail("        ! %s" % ln)
                if rc != 0:
                    ok = False
            for v in ver:
                budget = int(max(5, deadline - time.time()))
                out, err, rc, timed_out = run(v, repo_root, budget)
                detail("[verify %s] %s exit=%s%s: %s" % (entry.filename, "OK" if rc == 0 else "FAIL", rc, " TIMEOUT" if timed_out else "", v))
                for ln in (out or "").strip().splitlines()[-40:]:
                    detail("        | %s" % ln)
                if rc != 0:
                    ok = False

        record["state"] = "restored" if ok else "failed"
        sections.append(record)
        learner("--- %s — %s" % (label, "restored" if ok else "could not be fully restored"))

    # Sections that owed nothing (exempt / prose / no solution authored) are not
    # problems — only a section that had solutions and did not complete is.
    restored = [s for s in sections if s["state"] == "restored"]
    problems = [s for s in sections if s["state"] in ("failed", "skipped-budget", "missing")]
    if not problems:
        status = "ok"
    elif not restored:
        status = "failed"
    else:
        status = "partial"

    summary = {
        "status": status,
        "requested": until,
        "sections": sections,
        "restoredTo": len(target),
        "unrestored": [s["title"] for s in problems],
        "elapsedSeconds": round(time.time() - started, 1),
    }
    learner("=== Restore complete — %s ===" % status)
    return summary


def main():
    ap = argparse.ArgumentParser(description="Replay lab solutions to restore learner progress")
    ap.add_argument("--docs", required=True, help="path to the training's docs/ directory")
    ap.add_argument("--until", type=int, required=True, help="number of completed steps to replay")
    ap.add_argument("--training", default="", help="training key for multi-training repos")
    ap.add_argument("--summary-json", default="", help="write a machine-readable summary here")
    args = ap.parse_args()

    if not os.path.isdir(args.docs):
        detail("resume_replay: docs dir not found: %s" % args.docs)
        return 2
    if args.until <= 0:
        detail("resume_replay: nothing to replay (until=%d)" % args.until)
        return 0

    try:
        summary = replay(args.docs, args.until, args.training)
    except Exception as exc:  # never fail a provision because restore blew up
        detail("resume_replay: unexpected error: %r" % exc)
        summary = {"status": "failed", "requested": args.until, "sections": [], "unrestored": [], "error": repr(exc)}
        learner("=== Restore complete — failed ===")

    if args.summary_json:
        try:
            with open(args.summary_json, "w", encoding="utf-8") as fh:
                json.dump(summary, fh)
        except OSError as exc:
            detail("resume_replay: could not write summary: %r" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
