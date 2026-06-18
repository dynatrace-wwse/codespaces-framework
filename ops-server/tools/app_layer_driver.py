#!/usr/bin/env python3
"""app_layer_driver.py — Orbital app-layer test driver.

Runs INSIDE a provisioned lab container (the `dt` container of an
`app-layer-test` job). Reproduces the path the Enablement App's UI takes when a
learner clicks a check or "Run solution", but headless:

  STEP_SETUP / LAB_SOLUTION commands  -> `bash -lc`  (login shell: my_functions.sh
                                          loaded — same as orbitalService.exec(interactive=true))
  shell-verification command          -> `bash -c`   (NON-login: my_functions NOT
                                          loaded — same as exec(interactive=false))
  then evaluate stdout/exit against `expect` (exit-zero | contains | not-empty | gt)

Drives the full learner loop: STEP_SETUP (user actions) -> LAB_SOLUTION (apply +
verify the fix) -> shell-verification gates must then PASS. Exit 0 iff every gate
passes (and every solution verify exits 0).

Usage: python3 app_layer_driver.py <docsDir>
Mirrors scripts/lab-driver.mjs in dynatrace-app-enablements (kept in sync by intent).
"""
import os
import re
import subprocess
import sys

try:
    import yaml  # type: ignore
except Exception:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyyaml"], check=False)
    import yaml  # type: ignore

BLOCK_RE = re.compile(r"<!--\s*(LAB_QUESTION|STEP_SETUP|LAB_SOLUTION)\s*(.*?)-->", re.S)


def _md_files(docs_dir):
    return [os.path.join(docs_dir, f) for f in sorted(os.listdir(docs_dir)) if f.endswith(".md")]


def extract(docs_dir):
    """Return (setups, solutions, checks) in nav-ish (filename-sorted) order."""
    setups, solutions, checks = [], [], []
    for path in _md_files(docs_dir):
        fname = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for kind, body in BLOCK_RE.findall(text):
            try:
                doc = yaml.safe_load(body)
            except Exception:
                continue
            if not isinstance(doc, dict):
                continue
            if kind == "STEP_SETUP":
                for c in doc.get("commands", []) or []:
                    setups.append((fname, c))
            elif kind == "LAB_SOLUTION":
                cmds = doc.get("commands") or []
                ver = doc.get("verify") or []
                if cmds or ver:
                    solutions.append((fname, list(cmds), list(ver)))
            elif kind == "LAB_QUESTION" and doc.get("type") == "shell-verification":
                cmd = doc.get("command")
                exp = doc.get("expect") or {}
                if cmd and isinstance(exp, dict):
                    checks.append((fname, doc.get("buttonText") or doc.get("question") or "", cmd, exp))
    return setups, solutions, checks


def run(cmd, login):
    """Run a command; login=True -> `bash -lc` (sources .zshrc/my_functions)."""
    flag = "-lc" if login else "-c"
    p = subprocess.run(["bash", flag, cmd], capture_output=True, text=True)
    return p.stdout, p.stderr, p.returncode


def evaluate(stdout, exit_code, expect):
    out = (stdout or "").strip()
    op = expect.get("operator")
    val = expect.get("value")
    if op == "exit-zero":
        return exit_code == 0
    if op == "not-empty":
        return len(out) > 0
    if op == "contains":
        return str(val if val is not None else "") in out
    if op == "gt":
        try:
            return int(out.split()[0]) > int(val if val is not None else 0)
        except (ValueError, IndexError):
            return False
    return False


def main():
    if len(sys.argv) < 2:
        print("usage: app_layer_driver.py <docsDir>", file=sys.stderr)
        return 2
    docs_dir = sys.argv[1]
    setups, solutions, checks = extract(docs_dir)

    print(f"== STEP_SETUP ({len(setups)}) ==")
    for fname, c in setups:
        _, _, rc = run(c, login=True)
        print(f"  [setup {fname}] exit={rc}: {c}")

    print(f"== LAB_SOLUTION ({len(solutions)}) ==")
    solve_fail = 0
    for fname, cmds, ver in solutions:
        for c in cmds:
            _, _, rc = run(c, login=True)
            print(f"  [solve {fname}] exit={rc}: {c}")
        for v in ver:
            _, _, rc = run(v, login=True)
            ok = rc == 0
            solve_fail += 0 if ok else 1
            print(f"  [verify {fname}] {'OK' if ok else 'FAIL'} exit={rc}: {v}")

    print(f"== shell-verification ({len(checks)}) ==")
    passed = 0
    for fname, label, cmd, expect in checks:
        stdout, stderr, rc = run(cmd, login=False)  # non-interactive — the real app path
        ok = evaluate(stdout, rc, expect)
        passed += 1 if ok else 0
        out1 = (stdout or stderr or "").strip().replace("\n", " ")[:80]
        print(f"  {'PASS' if ok else 'FAIL'}  [{fname}] {label}")
        print(f"        $ {cmd}")
        print(f"        -> exit={rc} out={out1!r} expect={expect.get('operator')} {expect.get('value','')}")

    total = len(checks)
    print(f"\nRESULT: {passed}/{total} shell-verification checks passed; {solve_fail} solution-verify failures")
    ok_all = (passed == total) and (solve_fail == 0) and total > 0
    print("APP_LAYER_TEST: " + ("SUCCESS" if ok_all else "FAILURE"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
