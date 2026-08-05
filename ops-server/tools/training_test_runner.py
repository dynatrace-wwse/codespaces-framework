#!/usr/bin/env python3
"""training_test_runner.py — full-fidelity end-to-end training test through Orbital.

Unlike app_layer_driver.py (which runs INSIDE an already-provisioned container),
this runner exercises the EXACT path an Enablement App learner takes, from the
outside in:

  1. resolve the training in the arena catalog   (GET  /api/arena/trainings)
  2. provision a real session — token minting,
     per-user Grail isolation, daemon on amd64   (POST /api/arena/provision)
  3. wait for the environment to come up         (GET  /api/arena/sessions/{id})
  4. read the docs FROM the session (exec `cat docs/*.md`) so the steps tested
     always match the provisioned ref
  5. walk the SECTIONS (docs/*.md, doc order) exactly as a learner does —
     for each section, strictly in order:
       quiz validation       -> every LAB_QUESTION block parses per importer rules
       STEP_SETUP            -> exec  interactive=true
       LAB_SOLUTION commands -> exec-start + exec-status polling, LAB_WAIT=1
                                (the app's execLong path; BLOCKS until finished)
       LAB_SOLUTION verify   -> exec  interactive=true,  LAB_WAIT=1
       section checks        -> exec  interactive=false, LAB_WAIT=1
     then a per-section PASS/FAIL before moving to the next section — a
     failure names the exact section and step a learner would hit.
  6. TRAINING VERDICT: one line per section in learner order.
  7. terminate the session                       (POST /api/arena/sessions/{id}/terminate)

  Convention-driven: any training whose docs follow the framework grammar
  (docs/*.md + STEP_SETUP / LAB_SOLUTION / LAB_QUESTION annotation blocks) is
  testable with ZERO per-repo configuration. Golden example:
  enablement-kubernetes-101.

Prints TRAINING_TEST: SUCCESS|FAILURE and exits 0/1. All output is line-oriented
so the worker can stream it to the dashboard livelog.

Usage:
  python3 tools/training_test_runner.py --repo dynatrace-wwse/enablement-kubernetes-101 \
      [--ref main] [--orbital http://127.0.0.1:8080] [--keep-session] [--run-tag manual]

Environment (all optional):
  ORBITAL_API                       base URL (flag --orbital wins; default http://127.0.0.1:8080)
  TRAINING_TEST_TENANT_URL          DT tenant to provision against; unset -> worker static creds
  TRAINING_TEST_OAUTH_CLIENT_ID/…_SECRET   OAuth client for token minting on that tenant
  TRAINING_TEST_API_TOKEN           alternative: existing token with apiTokens.write
  TRAINING_TEST_REQUIRE_MINT=1      fail the run if token minting did not happen
  TRAINING_TEST_READY_TIMEOUT_S     provisioning wait cap (default 1500)
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Reuse the annotation-block parser + expect evaluator from the in-container
# driver — one grammar, two transports (kept in one place on purpose).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app_layer_driver import BLOCK_RE, evaluate, parse_block  # noqa: E402
from lab_nav import build_training_groups, page_is_exempt  # noqa: E402

DEFAULT_ORBITAL = "http://127.0.0.1:8080"
READY_TIMEOUT_S = int(os.environ.get("TRAINING_TEST_READY_TIMEOUT_S", "1500"))
READY_POLL_S = int(os.environ.get("TRAINING_TEST_READY_POLL_S", "15"))
EXEC_TIMEOUT_S = 120        # instant checks / setup commands
WAIT_TIMEOUT_S = 660        # LAB_WAIT-gated verifies (covers framework waitForPod 60x10s)
SOLVE_TIMEOUT_S = 840       # solution commands via exec-start (operator deploys)
SOLVE_POLL_S = int(os.environ.get("TRAINING_TEST_SOLVE_POLL_S", "5"))
FILE_MARK = "===TT_FILE:"

QUIZ_TYPES = ("multiple-choice", "dql-verification", "instructor-code", "shell-verification")


def log(msg=""):
    print(msg, flush=True)


# ── ANSI palette — same family as the framework's printInfo/printWarn so this
# log reads like postCreate/integration output. The env stream's separator
# lines end in an unterminated \033[35m (magenta), which would bleed into every
# following runner line — so every relayed line is suffixed with RESET, and the
# runner's own markers carry their color explicitly.
RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
GREY = "\033[0;90m"


def tag(ok, word_ok, word_fail="FAIL"):
    """Colored pass/fail word for step lines: green when ok, red when not."""
    return f"{GREEN}{word_ok}{RESET}" if ok else f"{RED}{word_fail}{RESET}"


def header(msg):
    log(f"{CYAN}{BOLD}{msg}{RESET}")


def step(n, msg):
    """Numbered [n/7] progress marker."""
    log(f"{CYAN}[{n}/7]{RESET} {msg}")


def error(msg):
    log(f"{RED}{msg}{RESET}")


def relay(prefix, line):
    """A line captured from the session (env log / command output): keep its own
    colors but force a RESET at the end so leaked codes never bleed onward."""
    log(f"{GREY}{prefix}{RESET} {line}{RESET}")


def orbital_bearer() -> str:
    """Service bearer for Orbital's /api/arena/* endpoints (arena auth).

    ORBITAL_TOKEN is in the environment when run under an ops service
    (systemd EnvironmentFile=/home/ops/.env — the training-test worker path);
    manual runs fall back to sudo-reading /home/ops/.env (same pattern as
    bootcamp_loadtest). Empty result = legacy caller: Orbital logs
    ARENA-LEGACY-CALLER during the compat window and 401s once
    ARENA_AUTH_ENFORCE=1.
    """
    tok = os.environ.get("ORBITAL_TOKEN", "")
    if not tok:
        try:
            out = subprocess.run(
                ["sudo", "-n", "grep", "-E", "^ORBITAL_TOKEN=", "/home/ops/.env"],
                capture_output=True, text=True)
            if out.returncode == 0 and "=" in out.stdout:
                tok = out.stdout.strip().split("=", 1)[1]
        except Exception:
            tok = ""
    return tok.split(",")[0].strip()  # comma-separated for rotation; send the first


class Orbital:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.bearer = orbital_bearer()

    def _req(self, method, path, body=None, timeout=30):
        req = urllib.request.Request(self.base + path, method=method)
        if self.bearer:
            req.add_header("Authorization", f"Bearer {self.bearer}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                return resp.status, json.loads(data) if data else {}
        except urllib.error.HTTPError as exc:
            data = exc.read()
            try:
                return exc.code, json.loads(data) if data else {}
            except ValueError:
                return exc.code, {"detail": data.decode(errors="replace")[:300]}

    def trainings(self):
        status, body = self._req("GET", "/api/arena/trainings")
        if status != 200:
            raise RuntimeError(f"GET /api/arena/trainings -> HTTP {status}: {body}")
        return body

    def provision(self, payload):
        status, body = self._req("POST", "/api/arena/provision", payload, timeout=120)
        if status != 200:
            raise RuntimeError(f"POST /api/arena/provision -> HTTP {status}: {body}")
        return body

    def session_status(self, job_id):
        _, body = self._req("GET", f"/api/arena/sessions/{job_id}")
        return body

    def livelog(self, job_id):
        """Raw text of the session's live provisioning log ('' when unavailable)."""
        import urllib.request
        try:
            with urllib.request.urlopen(f"{self.base}/api/jobs/{job_id}/livelog", timeout=15) as r:
                return r.read().decode(errors="replace")
        except Exception:
            return ""

    def exec(self, job_id, command, interactive=False, timeout_s=EXEC_TIMEOUT_S):
        """Synchronous exec — the app's normal check path."""
        status, body = self._req(
            "POST", f"/api/arena/sessions/{job_id}/exec",
            {"command": command, "interactive": interactive, "timeoutSeconds": timeout_s},
            timeout=timeout_s + 60,
        )
        if status != 200:
            return {"stdout": "", "stderr": f"HTTP {status}: {body.get('detail', body)}", "exitCode": -1}
        return body

    def exec_long(self, job_id, command, interactive=True, timeout_s=SOLVE_TIMEOUT_S):
        """Background exec via exec-start + exec-status — the app's execLong path
        for solution runs that outlive the app-function cap."""
        status, body = self._req(
            "POST", f"/api/arena/sessions/{job_id}/exec-start",
            {"command": command, "interactive": interactive, "timeoutSeconds": timeout_s},
        )
        if status != 200:
            return {"stdout": "", "stderr": f"HTTP {status}: {body.get('detail', body)}", "exitCode": -1}
        exec_id = body.get("execId", "")
        deadline = time.time() + timeout_s + 60
        while time.time() < deadline:
            time.sleep(SOLVE_POLL_S)
            status, res = self._req("GET", f"/api/arena/sessions/{job_id}/exec-status/{exec_id}")
            if status != 200:
                return {"stdout": "", "stderr": f"exec-status HTTP {status}", "exitCode": -1}
            if res.get("done"):
                return res
        return {"stdout": "", "stderr": f"exec-start {exec_id} still running after {timeout_s}s", "exitCode": -1}

    def terminate(self, job_id):
        return self._req("POST", f"/api/arena/sessions/{job_id}/terminate", {}, timeout=60)


# ── content extraction ──────────────────────────────────────────────────────

def extract_from_docs(files):
    """files: list of (fname, text). Returns (setups, solutions, checks, quizzes),
    every list in doc order. checks are shell-verification questions; quizzes are
    ALL LAB_QUESTION blocks (for structural validation)."""
    setups, solutions, checks, quizzes = [], [], [], []
    for fname, text in files:
        for kind, body in BLOCK_RE.findall(text):
            try:
                doc = parse_block(body)
            except Exception:
                doc = None
            if not isinstance(doc, dict):
                quizzes.append((fname, kind, {}, "unparseable block"))
                continue
            if kind == "STEP_SETUP":
                for c in doc.get("commands", []) or []:
                    setups.append((fname, c))
            elif kind == "LAB_SOLUTION":
                cmds = doc.get("commands") or []
                ver = doc.get("verify") or []
                if cmds or ver:
                    solutions.append((fname, list(cmds), list(ver)))
            elif kind == "LAB_QUESTION":
                problem = validate_question(doc)
                quizzes.append((fname, doc.get("type", "?"), doc, problem))
                if doc.get("type") == "shell-verification" and not problem:
                    checks.append((fname, doc.get("buttonText") or doc.get("question") or "",
                                   doc.get("command"), doc.get("expect") or {}))
    return setups, solutions, checks, quizzes


def validate_question(doc):
    """Mirror the importer's acceptance rules (api/import-lab.function.ts).
    Returns '' when the block is sound, else a human-readable problem — a block
    the importer would drop/skip is a step the learner can never complete."""
    qtype = doc.get("type")
    if qtype not in QUIZ_TYPES:
        return f"unknown question type '{qtype}'"
    if not doc.get("question") and qtype != "shell-verification":
        return "missing 'question'"
    if qtype == "multiple-choice":
        opts = doc.get("options")
        if not isinstance(opts, list) or not (2 <= len(opts) <= 6):
            return f"options must be a list of 2-6 (got {len(opts) if isinstance(opts, list) else 'none'})"
        try:
            correct = int(doc.get("correct"))
        except (TypeError, ValueError):
            return "missing/non-numeric 'correct' index"
        if not (0 <= correct < len(opts)):
            return f"'correct' index {correct} out of range"
    elif qtype == "dql-verification":
        if not doc.get("dql"):
            return "missing 'dql'"
        if not doc.get("buttonText"):
            return "missing 'buttonText'"
    elif qtype == "shell-verification":
        if not doc.get("command"):
            return "missing 'command'"
        exp = doc.get("expect")
        if not isinstance(exp, dict) or exp.get("operator") not in ("exit-zero", "contains", "not-empty", "gt"):
            return f"expect.operator must be exit-zero|contains|not-empty|gt (got {exp.get('operator') if isinstance(exp, dict) else exp})"
    return ""


def split_doc_dump(stdout):
    """Split the `cat docs/*.md` dump on FILE_MARK lines -> [(fname, text)]."""
    files = []
    current, buf = None, []
    for line in stdout.split("\n"):
        if line.startswith(FILE_MARK):
            if current is not None:
                files.append((current, "\n".join(buf)))
            current, buf = os.path.basename(line[len(FILE_MARK):].strip().rstrip("=")), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        files.append((current, "\n".join(buf)))
    return files


MKDOCS_MARK = "mkdocs.yaml"


def order_by_nav(files, mkdocs_text):
    """Reorder `[(fname, text)]` into mkdocs `nav:` order — the app's step order.

    The shell dump arrives in glob (lexical) order, which is not the order the
    learner walks: log-ingest's nav opens with `index.md` while lexical sort
    buries it last. Section numbering in this log, and the ordinals the resume
    feature replays against, both have to match the app. Pages absent from the
    nav keep their relative order at the end rather than disappearing.
    """
    if not mkdocs_text:
        return files, ""
    try:
        groups = build_training_groups(mkdocs_text)
    except Exception:
        return files, ""
    if not groups:
        return files, ""
    order = [os.path.basename(e.filename) for e in groups[0].entries]
    rank = {name: i for i, name in enumerate(order)}
    known = [f for f in files if f[0] in rank]
    unknown = [f for f in files if f[0] not in rank]
    known.sort(key=lambda f: rank[f[0]])
    return known + unknown, groups[0].key


def coverage_report(sections):
    """Which sections owe a LAB_SOLUTION and which have one.

    A page owes a solution when it asks the learner to *do* something — it has a
    STEP_SETUP or a shell-verification check. Pages that are prose, or that carry
    an explicit `<!-- LAB_NO_SOLUTION: … -->` marker (provisioning sanity probes
    like "did the container start?", or the "reproduce the bug" half of a
    reproduce/fix pair) owe nothing and are not counted as gaps.
    """
    owed, covered, exempt, gaps = 0, 0, 0, []
    for sec in sections:
        is_exempt, _reason = page_is_exempt(sec.get("text", ""))
        hands_on = bool(sec["setups"] or sec["checks"])
        if is_exempt:
            exempt += 1
            continue
        if not hands_on:
            continue
        owed += 1
        if sec["solutions"]:
            covered += 1
        else:
            gaps.append(sec["fname"])
    grade = "none" if owed == 0 and covered == 0 else ("complete" if not gaps else "partial")
    return {"owed": owed, "covered": covered, "exempt": exempt, "gaps": gaps, "grade": grade}


def with_wait(cmd):
    return f"export LAB_WAIT=1; {cmd}"


# ── phases ──────────────────────────────────────────────────────────────────

def doc_title(content):
    """First markdown H1/H2 in a doc — the section name a learner sees."""
    for ln in content.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:80]
    return ""

def wait_ready(orbital, job_id):
    """Poll session status AND stream the environment's own creation log inline
    ([env] prefix) — so a provisioning failure shows exactly which postCreate
    step died, in the same log as the rest of the run."""
    deadline = time.time() + READY_TIMEOUT_S
    last = ""
    seen = 0

    def stream_env_log():
        nonlocal seen
        text = orbital.livelog(job_id)
        if not text or len(text) <= seen:
            return
        for ln in text[seen:].splitlines():
            ln = ln.strip()
            if ln:
                relay("  [env]", ln)
        seen = len(text)

    while time.time() < deadline:
        st = orbital.session_status(job_id).get("status", "unknown")
        if st != last:
            color = GREEN if st == "ready" else (RED if st in ("failed", "terminated", "expired") else YELLOW)
            log(f"  session {job_id}: {color}{st}{RESET}")
            last = st
        stream_env_log()
        if st == "ready":
            return True
        if st in ("failed", "terminated", "expired"):
            stream_env_log()
            error(f"ERROR: session entered terminal state '{st}' during provisioning")
            return False
        time.sleep(READY_POLL_S)
    error(f"ERROR: session not ready after {READY_TIMEOUT_S}s")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="org/name of the training repo")
    ap.add_argument("--ref", default="", help="branch to test (default: catalog branch)")
    ap.add_argument("--orbital", default=os.environ.get("ORBITAL_API", DEFAULT_ORBITAL))
    ap.add_argument("--run-tag", default="manual", help="tag embedded in the test user id")
    ap.add_argument("--keep-session", action="store_true", help="skip terminate (debugging)")
    args = ap.parse_args()

    orbital = Orbital(args.orbital)
    started = time.time()
    header(f"=== TRAINING TEST — {args.repo} @ {args.ref or '(catalog branch)'} via {args.orbital} ===")

    # 1. Resolve training in the arena catalog (no hardcoded repo map — the
    #    catalog is the single source of truth, same list the app shows).
    trainings = orbital.trainings()
    training = next((t for t in trainings
                     if t.get("repoUrl", "").rstrip("/").endswith("/" + args.repo.split("/")[-1])), None)
    if training is None:
        error(f"ERROR: {args.repo} not in the arena catalog ({len(trainings)} trainings) — "
              "not an app-delivered training")
        log(f"{RED}{BOLD}TRAINING_TEST: FAILURE{RESET}")
        return 1
    step(1, f"catalog: id={training['id']} title={training.get('title', '')!r}")

    # 2. Provision — unique user per run so the one-session-per-user guard never
    #    dedupes onto a stale session, and per-user Grail isolation gets a fresh id.
    user_id = f"training-test+{args.run_tag}-{int(started)}@orbital.internal"
    payload = {"trainingId": training["id"], "userId": user_id}
    if args.ref:
        payload["ref"] = args.ref
    tenant = os.environ.get("TRAINING_TEST_TENANT_URL", "").rstrip("/")
    if tenant:
        payload["tenantUrl"] = tenant
        if os.environ.get("TRAINING_TEST_OAUTH_CLIENT_ID"):
            payload["oauthClientId"] = os.environ["TRAINING_TEST_OAUTH_CLIENT_ID"]
            payload["oauthClientSecret"] = os.environ.get("TRAINING_TEST_OAUTH_CLIENT_SECRET", "")
        elif os.environ.get("TRAINING_TEST_API_TOKEN"):
            payload["apiToken"] = os.environ["TRAINING_TEST_API_TOKEN"]
    prov = orbital.provision(payload)
    job_id = prov["jobId"]
    minted = bool(prov.get("tokenProvisioned"))
    step(2, f"provisioned session {job_id} as {user_id}")
    log(f"      tenant={tenant or '(worker static creds)'} tokenMinted={minted} "
        f"dtSessionId={prov.get('dtSessionId', '')} expiresAt={prov.get('expiresAt', '')}")
    # The mint story — names/ids only, never token values. This is the first
    # thing that breaks when a tenant's credentials or scopes are wrong.
    header(f"== TOKEN MINT (kind={prov.get('mintKind', 'unknown')}) ==")
    for t in prov.get("mintDetail") or []:
        log(f"  minted {GREEN}{t.get('envVar')}{RESET} (tokenId={t.get('tokenId')})")
    if prov.get("mintError"):
        error(f"  MINT ERROR: {prov['mintError']}")
    if minted and not prov.get("mintDetail"):
        log("  (token values supplied by caller — nothing minted by Orbital)")
    if not minted:
        log(f"  {YELLOW}no tokens minted — session will rely on worker static credentials{RESET}")
    if os.environ.get("TRAINING_TEST_REQUIRE_MINT") == "1" and not minted:
        error("ERROR: TRAINING_TEST_REQUIRE_MINT=1 but no token was minted")
        if not args.keep_session:
            orbital.terminate(job_id)
        log(f"{RED}{BOLD}TRAINING_TEST: FAILURE{RESET}")
        return 1

    failures = []
    try:
        # 3. Wait for the environment (cluster + operator + apps) to come up.
        step(3, f"waiting for session readiness (cap {READY_TIMEOUT_S}s)…")
        if not wait_ready(orbital, job_id):
            failures.append("session never became ready")
            log(f"{RED}{BOLD}TRAINING_TEST: FAILURE{RESET}")
            return 1
        log(f"      {GREEN}ready after {int(time.time() - started)}s{RESET}")

        # 4. Read the docs from inside the session — guaranteed to match the ref
        #    the environment was provisioned from.
        dump = orbital.exec(
            job_id,
            f'echo "{FILE_MARK}{MKDOCS_MARK}"; cat mkdocs.yaml 2>/dev/null || cat mkdocs.yml 2>/dev/null; '
            f'for f in docs/*.md; do echo "{FILE_MARK}$f"; cat "$f"; done',
            interactive=False, timeout_s=60,
        )
        if dump.get("exitCode") != 0:
            failures.append(f"could not read docs from session: {dump.get('stderr', '')[:200]}")
            log(f"{RED}{BOLD}TRAINING_TEST: FAILURE{RESET}")
            return 1
        files = split_doc_dump(dump.get("stdout", ""))
        mkdocs_text = next((t for n, t in files if n == MKDOCS_MARK), "")
        files = [(n, t) for n, t in files if n != MKDOCS_MARK]
        files, training_key = order_by_nav(files, mkdocs_text)
        setups, solutions, checks, quizzes = extract_from_docs(files)
        titles = {fname: doc_title(content) for fname, content in files}
        step(4, f"docs: {len(files)} files -> {len(setups)} setup cmds, "
             f"{len(solutions)} solutions, {len(checks)} shell checks, {len(quizzes)} question blocks")
        # The learner's path — sections (docs) strictly in doc order. Each
        # section is exercised exactly as a learner walks it: setup -> solution
        # (blocking, LAB_WAIT) -> solution verify -> the section's checks ->
        # verdict. A failure names the section and the exact step. This is
        # convention-driven (docs/*.md + STEP_SETUP / LAB_SOLUTION /
        # LAB_QUESTION blocks — golden example: enablement-kubernetes-101);
        # any training that follows the grammar is testable with zero config.
        sections = []
        for fname, text in files:
            f_setups, f_solutions, f_checks, f_quizzes = extract_from_docs([(fname, text)])
            sections.append({
                "fname": fname,
                "title": titles.get(fname, ""),
                "text": text,
                "setups": f_setups,
                "solutions": f_solutions,
                "checks": f_checks,
                "quizzes": f_quizzes,
            })
        coverage = coverage_report(sections)
        header("== LEARNER PATH ==")
        for i, sec in enumerate(sections, 1):
            log(f"  {i}. {sec['fname']}{' — ' + sec['title'] if sec['title'] else ''}"
                f"  (setup:{len(sec['setups'])} solutions:{len(sec['solutions'])}"
                f" checks:{len(sec['checks'])} quizzes:{len(sec['quizzes'])})")
        if not any(sec["checks"] or sec["solutions"] for sec in sections):
            failures.append("no shell-verification checks and no solutions found — nothing to test")

        # Automation coverage — how much of this training a machine can drive,
        # which is also how much of it the app can restore on resume. Reported,
        # never fatal: a training with gaps is still worth testing, it just
        # cannot claim end-to-end automation.
        log("")
        log(f"  coverage: {coverage['covered']}/{coverage['owed']} hands-on sections have solutions"
            f" ({coverage['exempt']} exempt) -> {coverage['grade']}")
        if coverage["gaps"]:
            log(f"  {YELLOW}no LAB_SOLUTION (and no LAB_NO_SOLUTION marker): "
                f"{', '.join(coverage['gaps'])}{RESET}")
            log(f"{YELLOW}{BOLD}TRAINING_TEST: COVERAGE_PARTIAL{RESET}")
        log("TRAINING_TEST: COVERAGE " + json.dumps({
            "grade": coverage["grade"], "covered": coverage["covered"],
            "owed": coverage["owed"], "exempt": coverage["exempt"],
            "gaps": coverage["gaps"], "trainingKey": training_key,
        }))

        # 5. Walk every section in order.
        section_results = []
        for i, sec in enumerate(sections, 1):
            fname = sec["fname"]
            sec_fail = []
            t0 = time.time()
            log("")
            header(f"== SECTION {i}/{len(sections)}: {fname}"
                   f"{' — ' + sec['title'] if sec['title'] else ''} ==")

            # Quiz blocks a learner would click through in this section.
            for _f, qtype, _doc, problem in sec["quizzes"]:
                if problem:
                    sec_fail.append(f"broken quiz ({qtype}): {problem}")
                    log(f"  [quiz] {RED}BROKEN{RESET} type={qtype}: {problem}")

            # STEP_SETUP — the actions the doc tells the learner to run first.
            for _f, c in sec["setups"]:
                r = orbital.exec(job_id, c, interactive=True, timeout_s=SOLVE_TIMEOUT_S)
                ok = r.get("exitCode") == 0
                log(f"  [setup] {tag(ok, 'ok')} exit={r.get('exitCode')}: {c}")
                if not ok:
                    sec_fail.append(f"setup failed: {c}")
                    for ln in (r.get("stdout") or "").strip().splitlines()[-8:]:
                        relay("        |", ln)

            # LAB_SOLUTION — run it and BLOCK until done (LAB_WAIT), then its
            # verify commands. Exactly the app's execLong path.
            for _f, cmds, ver in sec["solutions"]:
                for c in cmds:
                    r = orbital.exec_long(job_id, with_wait(c), interactive=True, timeout_s=SOLVE_TIMEOUT_S)
                    ok = r.get("exitCode") == 0
                    log(f"  [solve] {tag(ok, 'ok')} exit={r.get('exitCode')}: {c}")
                    if not ok:
                        sec_fail.append(f"solution failed: {c}")
                        for ln in (r.get("stdout") or "").strip().splitlines()[-8:]:
                            relay("        |", ln)
                for v in ver:
                    r = orbital.exec(job_id, with_wait(v), interactive=True, timeout_s=WAIT_TIMEOUT_S)
                    ok = r.get("exitCode") == 0
                    log(f"  [solution-verify] {tag(ok, 'OK')} exit={r.get('exitCode')}: {v}")
                    if not ok:
                        sec_fail.append(f"solution verify failed: {v}")
                        for ln in (r.get("stdout") or "").strip().splitlines()[-10:]:
                            relay("        |", ln)

            # The section's checks — after its solution finished, LAB_WAIT so
            # slow rollouts settle. What a learner's check click must show.
            for _f, label, cmd, expect in sec["checks"]:
                r = orbital.exec(job_id, with_wait(cmd), interactive=False, timeout_s=WAIT_TIMEOUT_S)
                ok = evaluate(r.get("stdout", ""), r.get("exitCode", -1), expect)
                log(f"  [check] {tag(ok, 'PASS')} {label} -> exit={r.get('exitCode')}"
                    f" expect={expect.get('operator')} {expect.get('value', '')}")
                if not ok:
                    sec_fail.append(f"check failed: {label}")
                    for ln in (r.get("stdout") or "").strip().splitlines()[-10:]:
                        relay("        |", ln)
                    if (r.get("stderr") or "").strip():
                        relay("        |", f"stderr: {r['stderr'].strip().splitlines()[-1][:200]}")

            ok = not sec_fail
            section_results.append((fname, sec["title"], ok, sec_fail, int(time.time() - t0)))
            verdict = f"{GREEN}{BOLD}SECTION PASS{RESET}" if ok else f"{RED}{BOLD}SECTION FAIL{RESET}"
            log(f"  -> {verdict} ({int(time.time() - t0)}s)"
                + ("" if ok else f" — {len(sec_fail)} problem(s)"))
            if not ok:
                failures.append(f"section {i} ({fname}): " + "; ".join(sec_fail[:3]))

        # 6. Training verdict — one line per section, in learner order.
        n_ok = sum(1 for _f, _t, ok, _e, _d in section_results if ok)
        log("")
        header(f"== TRAINING VERDICT ({n_ok}/{len(section_results)} sections passed) ==")
        for i, (fname, title, ok, sec_fail, dur_s) in enumerate(section_results, 1):
            log(f"  {i}. {tag(ok, 'PASS')}  {fname}"
                f"{' — ' + title if title else ''}  ({dur_s}s)"
                + ("" if ok else f"  {RED}[{'; '.join(sec_fail[:2])}]{RESET}"))

    finally:
        # 7. Always release the environment — a leaked session burns a worker
        #    slot for the full 2 h TTL.
        if args.keep_session:
            step(7, f"--keep-session: leaving {job_id} running")
        else:
            status, _ = orbital.terminate(job_id)
            step(7, f"terminated session {job_id} (HTTP {status})")

    dur = int(time.time() - started)
    if failures:
        log(f"\n{RED}RESULT: FAILURE after {dur}s — " + "; ".join(failures) + RESET)
        log(f"{RED}{BOLD}TRAINING_TEST: FAILURE{RESET}")
        return 1
    log(f"\n{GREEN}RESULT: all steps passed end-to-end in {dur}s "
        f"(provision -> mint -> steps -> solutions -> quizzes -> terminate){RESET}")
    log(f"{GREEN}{BOLD}TRAINING_TEST: SUCCESS{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
