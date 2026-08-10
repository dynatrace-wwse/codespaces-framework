"""The Virtual Classroom popup follows the app's theme.

The popup is a SEPARATE top-level window on Orbital's origin, so it cannot read
the Dynatrace app's `data-theme` — different document, different origin. It was
hardcoded navy and dark-only, so a learner on a light tenant opened a dark
window mid-workshop. The theme therefore has to arrive in the URL.

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_pad_theme.py
"""

import re

import dashboard.app as a

_TPL = a._PAD_PAGE_HTML


def _body_without_token_declarations() -> str:
    """The template minus the parts that SHOULD hold literal hex.

    Two exemptions, both deliberate:

    - the `:root` blocks, which are where the theme tokens are declared;
    - the brand mark, an inline copy of the app's own icon (a graduation cap in
      the Dynatrace gradient). A logo is the one thing on the page that must
      NOT change with the theme — it is the same mark in the app header, on the
      tile and here, and re-tinting it per theme would make it a different
      logo.
    """
    _, rest = _TPL.split(':root[data-theme="light"] {', 1)
    _, body = rest.split("}", 1)
    brand_start = body.index('<span id="brand-logo"')
    brand_end = body.index("</span>", body.index("</svg>", brand_start))
    return body[:brand_start] + body[brand_end:]


def test_the_brand_mark_is_the_only_exempt_colour():
    """Guards the exemption above: it must cover the logo and nothing else."""
    brand_start = _TPL.index('<span id="brand-logo"')
    brand_end = _TPL.index("</span>", _TPL.index("</svg>", brand_start))
    exempt = _TPL[brand_start:brand_end]
    assert "<svg" in exempt and "linearGradient" in exempt
    # It is a logo, not a layout: nothing themeable hides inside the exemption.
    assert "var(--" not in exempt


def test_both_themes_are_defined():
    assert ':root, :root[data-theme="dark"]' in _TPL
    assert ':root[data-theme="light"]' in _TPL


def test_no_colour_is_hardcoded_outside_the_token_blocks():
    # The whole point: a colour written inline cannot follow a theme.
    leftover = re.findall(r"#[0-9a-fA-F]{3,6}", _body_without_token_declarations())
    assert leftover == [], f"hardcoded colours still in the template: {sorted(set(leftover))}"


def test_the_two_themes_define_the_same_tokens():
    # A token defined in one theme and not the other renders as nothing at all.
    def tokens(block: str) -> set:
        return set(re.findall(r"(--[a-z0-9-]+):", block))
    dark = _TPL.split(':root, :root[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    light = _TPL.split(':root[data-theme="light"] {', 1)[1].split("}", 1)[0]
    assert tokens(dark) == tokens(light)
    assert "--bg" in tokens(dark) and "--accent" in tokens(dark)


def test_the_theme_is_stamped_before_first_paint():
    # Applied after render, a theme change is a visible flash of the wrong one.
    script_at = _TPL.index("document.documentElement.dataset.theme")
    body_at = _TPL.index("<body")
    assert script_at < body_at


def test_the_theme_parameter_is_whitelisted_not_interpolated():
    """`theme` is caller-supplied and lands inside the document."""
    import inspect
    src = inspect.getsource(a.live_pad_page)
    assert 'safe_theme = "light" if theme == "light" else "dark"' in src
    assert "__THEME__" in _TPL


def test_dark_is_the_default_for_a_caller_that_sends_no_theme():
    # An older app build sends no theme; it must keep today's appearance.
    import inspect
    assert 'theme: str = "dark"' in inspect.signature(a.live_pad_page).__str__() \
        or 'theme: str = "dark"' in inspect.getsource(a.live_pad_page)
