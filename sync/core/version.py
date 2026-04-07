"""Version parsing and bumping utilities.

Handles framework versions (X.Y.Z) and combined tags (vX.Y.Z_A.B.C).
"""

import re
from dataclasses import dataclass


@dataclass
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self):
        return f"{self.major}.{self.minor}.{self.patch}"

    def bump(self, part: str) -> "Version":
        if part == "major":
            return Version(self.major + 1, 0, 0)
        elif part == "minor":
            return Version(self.major, self.minor + 1, 0)
        elif part == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ValueError(f"Unknown version part: {part}")


@dataclass
class CombinedTag:
    framework: Version
    repo: Version

    def __str__(self):
        return f"v{self.framework}_{self.repo}"


_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_COMBINED_RE = re.compile(r"^v?(\d+\.\d+\.\d+)_(\d+\.\d+\.\d+)$")
_FW_VERSION_RE = re.compile(r'FRAMEWORK_VERSION="\$\{FRAMEWORK_VERSION:-(.+?)\}"')


def parse_version(s: str) -> Version:
    m = _SEMVER_RE.match(s.strip())
    if not m:
        raise ValueError(f"Invalid version: {s}")
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def parse_combined_tag(s: str) -> CombinedTag:
    m = _COMBINED_RE.match(s.strip())
    if not m:
        raise ValueError(f"Invalid combined tag: {s}")
    return CombinedTag(parse_version(m.group(1)), parse_version(m.group(2)))


def extract_framework_version(source_content: str) -> str:
    """Extract FRAMEWORK_VERSION from source_framework.sh content."""
    m = _FW_VERSION_RE.search(source_content)
    if not m:
        raise ValueError("FRAMEWORK_VERSION not found in source_framework.sh")
    return m.group(1)


def update_framework_version(source_content: str, new_version: str) -> str:
    """Update the FRAMEWORK_VERSION line in source_framework.sh content."""
    return _FW_VERSION_RE.sub(
        f'FRAMEWORK_VERSION="${{FRAMEWORK_VERSION:-{new_version}}}"',
        source_content,
    )
