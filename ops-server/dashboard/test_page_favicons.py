"""Every standalone page Orbital serves carries the enablement mark.

These pages are not part of the dashboard SPA — each one is its own top-level
document, opened in its own tab by a learner or a trainer mid-training. A tab
with no icon falls back to whatever the browser last associated with the
origin, which is how the Virtual Classroom spent months showing the ops
dashboard's grey hexagon, and how the pad header spent longer than that
showing the app's PREVIOUS icon.

The rule is easier to keep than to rediscover: a page that renders its own
`<head>` links the favicon. This test enumerates them from the source rather
than from a hand-kept list, so a page added tomorrow fails here instead of
shipping iconless.

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_page_favicons.py
"""

import re

import dashboard.app as a

FAVICON = '<link rel="icon" type="image/png" sizes="32x32" href="/static/images/favicon-32.png">'

_SRC = open(a.__file__, encoding="utf-8").read()


def _documents() -> list:
    """Every standalone HTML document in app.py, keyed by its <title>.

    Keying on the title is what makes this self-maintaining: a new page has to
    have one, and the title is also the human-readable name of the thing that
    would be broken.
    """
    out = []
    for m in re.finditer(r"<title>(.*?)</title>", _SRC, re.S):
        # The document this title belongs to: back to the nearest <head>, and
        # forward far enough to cover the rest of the head's links.
        head_at = _SRC.rfind("<head", 0, m.start())
        end = _SRC.find("</head>", m.end())
        if head_at == -1 or end == -1:
            continue
        out.append((m.group(1).strip(), _SRC[head_at:end]))
    return out


def test_every_standalone_page_links_the_favicon():
    missing = [title for title, head in _documents() if 'rel="icon"' not in head]
    assert missing == [], f"pages served without a favicon: {missing}"


def test_they_all_point_at_the_same_asset():
    """A second copy of the mark is a second thing to forget to update — the
    exact failure that had the pad showing a stale icon."""
    hrefs = set(
        re.findall(r'rel="icon"[^>]*href="([^"]+)"', head)[0]
        for _, head in _documents()
    )
    assert hrefs == {"/static/images/favicon-32.png"}, hrefs


def test_the_asset_exists_and_is_a_png():
    """`/static` is the only asset origin these pages can reach, and a 404
    favicon is indistinguishable from no favicon at all."""
    import pathlib
    icon = pathlib.Path(a.__file__).parent / "static" / "images" / "favicon-32.png"
    assert icon.is_file()
    assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
