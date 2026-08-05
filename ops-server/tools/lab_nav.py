#!/usr/bin/env python3
"""lab_nav.py — mkdocs nav enumerator, ordinal-compatible with the Enablement App.

The app numbers a training's steps from the `nav:` array of `mkdocs.yaml`
(see `api/import-lab.function.ts` — `extractNavEntries` / `buildTrainingGroups`).
Every solution runner in this repo historically enumerated `docs/*.md` by
*filename sort* instead, which is a different order: log-ingest-101's nav starts
with `index.md`, filename sort puts it last. That divergence is harmless while
you only ever run the whole training, and wrong the moment you run a prefix of it
("restore the learner up to step 3"), so this module is the single shared
enumerator for anything that needs to agree with the app.

Deliberately dependency-free: this also runs inside the lab container, whose
python3 is externally-managed (PEP 668) and ships no pyyaml. Only the nav shapes
actually authored across the fleet are supported:

    nav:
      - "Welcome": index.md                      # simple entry
      - index.md                                 # bare filename
      - "Section":                               # nested block section
          - "Sub": sub.md
      - "Section": [{'A': 'a.md'}, {'B': 'b.md'}] # nested flow section

Multi-training repos (every top-level nav item is a pure section) split into one
training per top-level section, matching `buildTrainingGroups`.
"""
import os
import re

__all__ = [
    "NavEntry",
    "TrainingGroup",
    "parse_nav",
    "build_training_groups",
    "nav_entries",
    "find_mkdocs",
    "NO_SOLUTION_RE",
    "page_is_exempt",
]

# Authors mark a page that legitimately owes no LAB_SOLUTION — a provisioning
# sanity probe ("did the container come up?"), a prose/landing page, or the
# "reproduce the bug" half of a reproduce/fix pair. The reason text is surfaced
# in Orbital's fleet tooltip, so it is captured, not just matched.
NO_SOLUTION_RE = re.compile(r"<!--\s*LAB_NO_SOLUTION\s*:?\s*(.*?)-->", re.S)


class NavEntry:
    """One step, in app ordinal order."""

    __slots__ = ("index", "title", "filename", "group")

    def __init__(self, index, title, filename, group=""):
        self.index = index
        self.title = title
        self.filename = filename
        self.group = group

    def __repr__(self):  # pragma: no cover - debugging aid
        return "NavEntry(%d, %r, %r, %r)" % (self.index, self.title, self.filename, self.group)

    def __eq__(self, other):
        return (
            isinstance(other, NavEntry)
            and (self.index, self.title, self.filename, self.group)
            == (other.index, other.title, other.filename, other.group)
        )


class TrainingGroup:
    """A training derived from one repo: the whole repo, or one top-level section."""

    __slots__ = ("key", "title", "entries")

    def __init__(self, key, title, entries):
        self.key = key
        self.title = title
        self.entries = entries


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


_FLOW_PAIR_RE = re.compile(r"(['\"])(.*?)\1\s*:\s*(['\"])(.*?)\3")
_FLOW_BARE_RE = re.compile(r"(['\"])([^'\"]+?\.md)\1")


def _parse_flow_section(text):
    """Parse an inline flow sequence: [{'Title': 'file.md'}, …] or ['a.md', 'b.md']."""
    pairs = _FLOW_PAIR_RE.findall(text)
    if pairs:
        return [("entry", p[1], p[3]) for p in pairs]
    return [("entry", m[1], m[1]) for m in _FLOW_BARE_RE.findall(text)]


def _nav_block_lines(raw):
    """Return the physical lines belonging to the top-level `nav:` mapping."""
    lines = raw.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^nav\s*:\s*(#.*)?$", line):
            start = i + 1
            break
    if start is None:
        raise ValueError("mkdocs config has no 'nav' key")
    out = []
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(line)
            continue
        # A non-indented, non-list line ends the nav mapping.
        if not line[0].isspace() and not line.lstrip().startswith("- "):
            break
        out.append(line)
    return out


def _indent(line):
    return len(line) - len(line.lstrip())


def _parse_items(lines, pos, base_indent):
    """Recursive-descent over `- ` items at `base_indent`. Returns (items, pos)."""
    items = []
    n = len(lines)
    while pos < n:
        line = lines[pos]
        if not line.strip() or line.lstrip().startswith("#"):
            pos += 1
            continue
        ind = _indent(line)
        if ind < base_indent or not line.lstrip().startswith("- "):
            break
        body = line.lstrip()[2:].strip()
        pos += 1

        # Titles routinely contain colons ("1. Bug: Clear Completed",
        # "Hands-on: Deployment/Configuration"), so a quoted title has to be
        # consumed as a unit before splitting on the key/value colon.
        m = re.match(r"^(['\"])(.*?)\1\s*:\s*(.*)$", body)
        if m:
            title, value = m.group(2), m.group(3).strip()
        else:
            m = re.match(r"^(.*?)\s*:\s*(.*)$", body)
            if not m:
                # Bare filename entry: `- index.md`
                fname = _unquote(body)
                if fname:
                    items.append(("entry", fname, fname))
                continue
            title, value = _unquote(m.group(1)), m.group(2).strip()
        if value.startswith("["):
            children = _parse_flow_section(value)
            items.append(("section", title, children))
        elif value:
            items.append(("entry", title, _unquote(value)))
        else:
            # Nested block section — children are the deeper-indented items.
            child_indent = None
            for look in range(pos, n):
                if lines[look].strip() and not lines[look].lstrip().startswith("#"):
                    child_indent = _indent(lines[look])
                    break
            if child_indent is not None and child_indent > ind:
                children, pos = _parse_items(lines, pos, child_indent)
                items.append(("section", title, children))
            else:
                items.append(("section", title, []))
    return items, pos


def parse_nav(raw):
    """Parse mkdocs.yaml text into a nav tree of ('entry'|'section', title, …)."""
    lines = _nav_block_lines(raw)
    items, _ = _parse_items(lines, 0, _indent(next(l for l in lines if l.strip())))
    if not items:
        raise ValueError("could not extract any navigation entries from mkdocs config")
    return items


def _flatten(items, out):
    for kind, title, value in items:
        if kind == "entry":
            out.append((title, value))
        else:
            _flatten(value, out)
    return out


def _slugify(title):
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", title.lower()))


def build_training_groups(raw, site_name=""):
    """Mirror of `buildTrainingGroups`: multi-training when every top-level item is a section."""
    items = parse_nav(raw)
    if items and all(kind == "section" for kind, _, _ in items):
        groups = []
        for _, title, children in items:
            flat = _flatten(children, [])
            groups.append(
                TrainingGroup(
                    _slugify(title),
                    title,
                    [NavEntry(i, t, f, _slugify(title)) for i, (t, f) in enumerate(flat)],
                )
            )
        return groups
    flat = _flatten(items, [])
    return [TrainingGroup("", site_name, [NavEntry(i, t, f) for i, (t, f) in enumerate(flat)])]


def find_mkdocs(docs_dir):
    """Locate the mkdocs config that owns `docs_dir` (its sibling), or None."""
    parent = os.path.dirname(os.path.abspath(docs_dir.rstrip("/")))
    for name in ("mkdocs.yaml", "mkdocs.yml"):
        path = os.path.join(parent, name)
        if os.path.isfile(path):
            return path
    return None


def nav_entries(docs_dir, training_key=""):
    """Ordered NavEntry list for `docs_dir`, app-compatible.

    Falls back to filename sort when no mkdocs config is readable, so callers
    keep working on repos that predate the nav contract.
    """
    path = find_mkdocs(docs_dir)
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            groups = build_training_groups(raw)
            if training_key:
                for g in groups:
                    if g.key == training_key:
                        return g.entries
            # Default to the first group; single-training repos have exactly one.
            # Entries for pages missing on disk are kept: the importer substitutes
            # placeholder content rather than dropping the step, so dropping here
            # would shift every later ordinal out of agreement with the app.
            return groups[0].entries if groups else []
        except Exception:
            pass
    names = sorted(f for f in os.listdir(docs_dir) if f.endswith(".md"))
    return [NavEntry(i, n, n) for i, n in enumerate(names)]


def page_is_exempt(text):
    """(is_exempt, reason) for a page carrying `<!-- LAB_NO_SOLUTION: … -->`."""
    m = NO_SOLUTION_RE.search(text or "")
    if not m:
        return False, ""
    return True, (m.group(1) or "").strip()
