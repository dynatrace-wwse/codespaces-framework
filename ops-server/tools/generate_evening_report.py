#!/usr/bin/env python3
"""
Generate a combined evening run report from stress test + validation + nightly results.

Usage:
  python3 generate_evening_report.py --log-dir /tmp/evening-YYYYMMDD
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Stress test report section
# ---------------------------------------------------------------------------

def format_stress_section(json_path: Path) -> str:
    if not json_path.exists():
        return f"_Stress test result not found: {json_path}_\n"
    data = json.loads(json_path.read_text())
    arch = data.get("arch", "?")
    repo = data.get("repo", "?").split("/")[-1]
    sat_point = data.get("saturation_point", "?")
    sat_reason = data.get("saturation_reason", "?")
    started = data.get("started_at", "?")
    finished = data.get("finished_at", "?")

    lines = [
        f"### Arch: `{arch}` — repo `{repo}`",
        f"",
        f"| Field | Value |",
        f"|---|---|",
        f"| Started | {started} |",
        f"| Finished | {finished} |",
        f"| Max jobs tested | {data.get('max_jobs', '?')} |",
        f"| Step | {data.get('step', '?')} |",
        f"| Wave duration | {data.get('wave_minutes', '?')} min |",
        f"| **Saturation point** | **{sat_point} concurrent jobs** |",
        f"| Saturation reason | {sat_reason} |",
        f"",
        f"**Wave progression:**",
        f"",
        f"| Wave | Concurrency | Worker | CPU% | Mem% | Active Jobs |",
        f"|---|---|---|---|---|---|",
    ]

    for wave in data.get("waves", []):
        conc = wave["concurrency"]
        for poll in wave.get("polls", [])[-1:]:  # last poll per wave
            for w in poll.get("workers", []):
                lines.append(
                    f"| {conc} | {conc} | {w.get('worker_id','?')[:20]} | "
                    f"{w.get('cpu_pct',0):.1f} | {w.get('mem_pct',0):.1f} | "
                    f"{w.get('active_jobs',0)} |"
                )

    # Peak metrics
    peak_cpu: dict[str, float] = {}
    peak_mem: dict[str, float] = {}
    for wave in data.get("waves", []):
        for poll in wave.get("polls", []):
            for w in poll.get("workers", []):
                wid = w.get("worker_id", "?")
                peak_cpu[wid] = max(peak_cpu.get(wid, 0), float(w.get("cpu_pct", 0)))
                peak_mem[wid] = max(peak_mem.get(wid, 0), float(w.get("mem_pct", 0)))

    lines += ["", "**Peak metrics per worker:**", ""]
    lines += ["| Worker | Peak CPU% | Peak Mem% |", "|---|---|---|"]
    for wid in sorted(peak_cpu):
        lines.append(f"| {wid[:24]} | {peak_cpu[wid]:.1f} | {peak_mem.get(wid, 0):.1f} |")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation summary section
# ---------------------------------------------------------------------------

def format_validation_section(log_path: Path, report_dir: Path) -> str:
    lines = [""]

    # Parse validation log for per-repo results
    repo_results: list[dict] = []
    if log_path.exists():
        current_repo = None
        passes = fails = exp_fails = 0
        for line in log_path.read_text().splitlines():
            if "Validating:" in line:
                if current_repo:
                    repo_results.append({"repo": current_repo, "pass": passes, "fail": fails, "exp_fail": exp_fails})
                parts = line.split("Validating:")[-1].strip()
                current_repo = parts.split("(")[0].strip() if "(" in parts else parts
                passes = fails = exp_fails = 0
            elif "Results:" in line and current_repo:
                # "Results: N pass, N fail, N expected-fail, N skip"
                import re
                m = re.search(r"(\d+) pass.*?(\d+) fail.*?(\d+) expected", line)
                if m:
                    passes, fails, exp_fails = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if current_repo:
            repo_results.append({"repo": current_repo, "pass": passes, "fail": fails, "exp_fail": exp_fails})

    # Also scan validation-reports/ dir for markdown files
    if report_dir.exists():
        for rdir in sorted(report_dir.iterdir()):
            if not rdir.is_dir():
                continue
            # Find today's report
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            md = rdir / f"{date_str}.md"
            if not md.exists():
                # Take latest
                mds = sorted(rdir.glob("*.md"), reverse=True)
                if mds:
                    md = mds[0]
            if not md.exists():
                continue
            content = md.read_text()
            # Extract pass/fail from markdown table
            import re
            p = re.search(r"\| Pass \| (\d+) \|", content)
            f = re.search(r"\| Fail \| \*\*(\d+)\*\* \|", content)
            e = re.search(r"\| Expected-fail \| (\d+) \|", content)
            if p and f:
                repo_name = rdir.name
                if not any(r["repo"] in repo_name or repo_name in r["repo"] for r in repo_results):
                    repo_results.append({
                        "repo": repo_name,
                        "pass": int(p.group(1)),
                        "fail": int(f.group(1)),
                        "exp_fail": int(e.group(1)) if e else 0,
                    })

    if not repo_results:
        lines.append("_No validation results found — check validation.log_")
        return "\n".join(lines)

    total_p = sum(r["pass"] for r in repo_results)
    total_f = sum(r["fail"] for r in repo_results)
    total_repos = len(repo_results)
    clean_repos = sum(1 for r in repo_results if r["fail"] == 0)

    lines += [
        f"**Summary:** {clean_repos}/{total_repos} repos fully passing | {total_p} pass, {total_f} fail",
        f"",
        f"| Repo | Pass | Fail | Exp-Fail | Status |",
        f"|---|---|---|---|---|",
    ]
    for r in sorted(repo_results, key=lambda x: (-x["fail"], x["repo"])):
        status = "✅ PASS" if r["fail"] == 0 else "❌ FAIL"
        lines.append(
            f"| {r['repo']} | {r['pass']} | {r['fail']} | {r['exp_fail']} | {status} |"
        )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Nightly build section
# ---------------------------------------------------------------------------

def format_nightly_section(log_path: Path) -> str:
    lines = [""]
    if not log_path.exists():
        lines.append("_Nightly log not found_")
        return "\n".join(lines)

    content = log_path.read_text()
    # Pull summary lines from the log
    queued: list[str] = []
    for line in content.splitlines():
        if "Queued:" in line:
            queued.append(line.split("Queued:")[-1].strip())

    lines.append(f"**Total repos queued:** {len(queued)}")
    lines.append("")
    if queued:
        lines.append("| Repo | Queue |")
        lines.append("|---|---|")
        for q in queued:
            parts = q.split("→")
            repo = parts[0].strip() if parts else q
            queue = parts[1].strip() if len(parts) > 1 else ""
            lines.append(f"| {repo} | {queue} |")

    lines.append("")
    lines.append(
        "_Note: Integration test results will appear in the dashboard as jobs complete._"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--log-dir", required=True)
    p.add_argument("--output", default=None)
    args = p.parse_args()

    log_dir = Path(args.log_dir)
    out = Path(args.output) if args.output else log_dir / "EVENING_REPORT.md"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    framework_root = Path(__file__).resolve().parent.parent.parent

    lines = [
        f"# Evening Run Report — {now}",
        f"",
        f"**Log directory:** `{log_dir}`",
        f"",
        f"---",
        f"",
        f"## 1. Stress Test Results",
        f"",
    ]

    amd_json = log_dir / "stress-amd64.json"
    arm_json = log_dir / "stress-arm64.json"

    if amd_json.exists():
        lines.append(format_stress_section(amd_json))
    else:
        lines.append("_AMD64 stress test did not run or result missing._\n")

    if arm_json.exists():
        lines.append(format_stress_section(arm_json))
    else:
        lines.append("_ARM64 stress test did not run or result missing._\n")

    lines += [
        "### Assessment",
        "",
        "- **AMD workers** (2 × 6-slot Sysbox pool): saturation point above shows maximum safe concurrency.",
        "- **ARM master** (1 × 5-slot Sysbox pool): protected at 70% CPU and 70% Mem.",
        "- Pre-warmed Sysbox slots eliminate 60–120s cold-start overhead — job startup now ≈ 15s.",
        "",
        "---",
        "",
        "## 2. Agentic Validation",
        "",
    ]
    val_log = log_dir / "validation.log"
    val_report_dir = framework_root / "validation-reports"
    lines.append(format_validation_section(val_log, val_report_dir))

    lines += [
        "---",
        "",
        "## 3. Nightly Build",
        "",
    ]
    lines.append(format_nightly_section(log_dir / "nightly.log"))

    lines += [
        "---",
        "",
        f"_Report generated: {now}_",
    ]

    out.write_text("\n".join(lines))
    print(f"Report written: {out}")


if __name__ == "__main__":
    main()
