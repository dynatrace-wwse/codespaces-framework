"""Drift test for _dt_hostgroup — the per-user Grail-isolation id.

The same function is duplicated in three separately-deployed processes
(dashboard/app.py, workers/manager.py, worker-agent/executor.py) because they
are three deploy units, and mirrored a fourth time in the app
(ui/app/training/utils/templateVars.ts). If any copy drifts, the worker's
DT_HOSTGROUP and the app's {{DT_SESSION_ID}} stop agreeing and every
`endsWith(k8s.cluster.name, …)` filter in every training silently misses.

AST comparison on purpose: importing manager.py / executor.py drags in redis and
the whole worker stack, which this must not need. Docstrings are stripped — they
legitimately differ per process; the logic must not.

Run:  python3 -m pytest dashboard/test_hostgroup.py
  or: python3 dashboard/test_hostgroup.py
"""
import ast
import pathlib

OPS = pathlib.Path(__file__).resolve().parent.parent
MIRRORS = [
    OPS / "dashboard" / "app.py",
    OPS / "workers" / "manager.py",
    OPS / "worker-agent" / "executor.py",
]


def _body(path):
    """AST dump of _dt_hostgroup's statements, docstring stripped."""
    tree = ast.parse(path.read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_dt_hostgroup"), None)
    assert fn, f"_dt_hostgroup not found in {path}"
    stmts = fn.body
    if (stmts and isinstance(stmts[0], ast.Expr)
            and isinstance(stmts[0].value, ast.Constant)
            and isinstance(stmts[0].value.value, str)):
        stmts = stmts[1:]
    return "\n".join(ast.dump(s) for s in stmts)


def test_all_three_mirrors_are_identical():
    bodies = {p.name: _body(p) for p in MIRRORS}
    first = next(iter(bodies.values()))
    for name, body in bodies.items():
        assert body == first, f"{name} has drifted from the other _dt_hostgroup mirrors"


def test_every_mirror_bounds_the_local_part():
    # 17 (local) + 1 + 8 (YYYYMMDD) = 26, which still leaves >= 10 repo chars
    # under the framework's 37-char DynaKube name cap.
    for p in MIRRORS:
        src = p.read_text()
        assert "HOSTGROUP_LOCAL_MAX = 17" in src, f"{p.name} lost the bound"
        assert 'user[:HOSTGROUP_LOCAL_MAX].rstrip("-")' in src, f"{p.name} does not apply the bound"


def test_app_mirror_matches():
    ts = (OPS.parent.parent / "dynatrace-app-enablements" / "ui" / "app" / "training"
          / "utils" / "templateVars.ts")
    if not ts.exists():          # the app repo is not always checked out beside us
        return
    src = ts.read_text()
    assert "HOSTGROUP_LOCAL_MAX = 17" in src
    assert "slice(0, HOSTGROUP_LOCAL_MAX)" in src


if __name__ == "__main__":
    test_all_three_mirrors_are_identical()
    test_every_mirror_bounds_the_local_part()
    test_app_mirror_matches()
    print("ok — _dt_hostgroup mirrors agree")
