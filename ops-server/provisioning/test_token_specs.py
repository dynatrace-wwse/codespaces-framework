"""Tests for the token-spec parser — in particular the `kind` field.

A repo declares what tokens its training needs. Getting `kind` wrong is silent in the
worst way: gen3 scope names sent to the classic API are dropped rather than rejected, so
the session gets a token that authenticates and can do nothing.

Run: /home/ops/ops-venv/bin/python -m provisioning.test_token_specs
  or pytest provisioning/test_token_specs.py
"""
from .token_specs import _parse_yaml, DEFAULT_SPECS


def test_kind_defaults_to_classic_and_aliases_default_empty():
    specs = _parse_yaml("""
tokens:
  - name_suffix: operator
    env_var: DT_OPERATOR_TOKEN
    scopes: [entities.read]
""")
    assert len(specs) == 1
    assert specs[0].kind == "classic"
    assert specs[0].aliases == []


def test_platform_kind_and_aliases_are_read():
    specs = _parse_yaml("""
tokens:
  - name_suffix: api
    env_var: DT_API_TOKEN
    aliases: [DT_BIZEVENTS_TOKEN]
    scopes: [ReadConfig, bizevents.ingest]
  - name_suffix: platform
    env_var: DT_PLATFORM_TOKEN
    kind: platform
    scopes: [document:documents:read]
""")
    assert [s.kind for s in specs] == ["classic", "platform"]
    assert specs[0].aliases == ["DT_BIZEVENTS_TOKEN"]
    assert specs[1].scopes == ["document:documents:read"]


def test_unknown_kind_falls_back_to_classic_rather_than_dropping_the_token():
    # A missing token fails the session; a classic one fails at its first call with a
    # real error. The second is the better failure.
    specs = _parse_yaml("""
tokens:
  - name_suffix: x
    env_var: DT_X
    kind: Platfrom
    scopes: [entities.read]
""")
    assert len(specs) == 1
    assert specs[0].kind == "classic"


def test_kind_is_case_and_whitespace_insensitive():
    specs = _parse_yaml("""
tokens:
  - name_suffix: p
    env_var: DT_P
    kind: "  Platform  "
    scopes: [document:documents:read]
""")
    assert specs[0].kind == "platform"


def test_empty_file_falls_back_to_the_framework_default():
    assert _parse_yaml("tokens: []") == DEFAULT_SPECS


def test_the_real_astroshop_declaration_satisfies_its_own_gate():
    # post-create.sh runs bootstrapWorkshop only when DT_ENVIRONMENT *and* DT_API_TOKEN
    # *and* DT_PLATFORM_TOKEN are all set. Nothing declared the last two, so every
    # session ever delivered came up as an empty dev container with no error.
    from pathlib import Path
    yaml_path = Path(__file__).resolve().parents[3] / "demo-astroshop-problems" \
        / ".devcontainer" / "yaml" / "dt-tokens.yaml"
    if not yaml_path.exists():
        return  # sibling repo not checked out on this host — not a failure
    specs = _parse_yaml(yaml_path.read_text())
    by_var = {s.env_var: s for s in specs}
    assert "DT_API_TOKEN" in by_var and by_var["DT_API_TOKEN"].kind == "classic"
    assert "DT_PLATFORM_TOKEN" in by_var and by_var["DT_PLATFORM_TOKEN"].kind == "platform"
    # The operator still needs its own pair — bootstrapWorkshop phase 1 skips otherwise.
    assert {"DT_OPERATOR_TOKEN", "DT_INGEST_TOKEN"} <= set(by_var)
    # Every platform scope must already be in gen3 vocabulary; a classic name here would
    # be silently dropped by to_platform_scopes.
    assert all(":" in s for s in by_var["DT_PLATFORM_TOKEN"].scopes)


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
