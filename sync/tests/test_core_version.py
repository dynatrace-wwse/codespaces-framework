"""Tests for sync.core.version — Version parsing, bumping, combined tags."""

import pytest

from sync.core.version import (
    Version,
    CombinedTag,
    parse_version,
    parse_combined_tag,
    extract_framework_version,
    update_framework_version,
)


# ---------------------------------------------------------------------------
# Version dataclass
# ---------------------------------------------------------------------------

class TestVersion:
    def test_str(self):
        v = Version(1, 2, 3)
        assert str(v) == "1.2.3"

    def test_str_zero(self):
        v = Version(0, 0, 0)
        assert str(v) == "0.0.0"

    def test_bump_patch(self):
        v = Version(1, 2, 3)
        new = v.bump("patch")
        assert str(new) == "1.2.4"

    def test_bump_minor(self):
        v = Version(1, 2, 3)
        new = v.bump("minor")
        assert str(new) == "1.3.0"

    def test_bump_major(self):
        v = Version(1, 2, 3)
        new = v.bump("major")
        assert str(new) == "2.0.0"

    def test_bump_invalid(self):
        v = Version(1, 2, 3)
        with pytest.raises(ValueError, match="Unknown version part"):
            v.bump("micro")

    def test_bump_patch_resets_nothing(self):
        v = Version(1, 2, 3).bump("patch")
        assert v.major == 1
        assert v.minor == 2
        assert v.patch == 4

    def test_bump_minor_resets_patch(self):
        v = Version(1, 2, 3).bump("minor")
        assert v.patch == 0

    def test_bump_major_resets_minor_and_patch(self):
        v = Version(1, 2, 3).bump("major")
        assert v.minor == 0
        assert v.patch == 0

    def test_immutability(self):
        """Bumping returns a new Version, doesn't mutate original."""
        v = Version(1, 2, 3)
        _ = v.bump("patch")
        assert str(v) == "1.2.3"


# ---------------------------------------------------------------------------
# CombinedTag
# ---------------------------------------------------------------------------

class TestCombinedTag:
    def test_str(self):
        ct = CombinedTag(Version(1, 2, 5), Version(1, 0, 0))
        assert str(ct) == "v1.2.5_1.0.0"

    def test_str_with_zeros(self):
        ct = CombinedTag(Version(0, 0, 1), Version(0, 0, 0))
        assert str(ct) == "v0.0.1_0.0.0"


# ---------------------------------------------------------------------------
# parse_version
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_plain(self):
        v = parse_version("1.2.3")
        assert v == Version(1, 2, 3)

    def test_with_v_prefix(self):
        v = parse_version("v1.2.3")
        assert v == Version(1, 2, 3)

    def test_with_whitespace(self):
        v = parse_version("  1.2.3  ")
        assert v == Version(1, 2, 3)

    def test_large_numbers(self):
        v = parse_version("100.200.300")
        assert v == Version(100, 200, 300)

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="Invalid version"):
            parse_version("")

    def test_invalid_two_parts(self):
        with pytest.raises(ValueError, match="Invalid version"):
            parse_version("1.2")

    def test_invalid_four_parts(self):
        with pytest.raises(ValueError, match="Invalid version"):
            parse_version("1.2.3.4")

    def test_invalid_alpha(self):
        with pytest.raises(ValueError, match="Invalid version"):
            parse_version("abc")

    def test_invalid_combined_tag(self):
        """A combined tag should fail parse_version."""
        with pytest.raises(ValueError, match="Invalid version"):
            parse_version("v1.2.3_1.0.0")


# ---------------------------------------------------------------------------
# parse_combined_tag
# ---------------------------------------------------------------------------

class TestParseCombinedTag:
    def test_basic(self):
        ct = parse_combined_tag("v1.2.5_1.0.0")
        assert ct.framework == Version(1, 2, 5)
        assert ct.repo == Version(1, 0, 0)

    def test_without_v_prefix(self):
        ct = parse_combined_tag("1.2.5_1.0.0")
        assert ct.framework == Version(1, 2, 5)
        assert ct.repo == Version(1, 0, 0)

    def test_with_whitespace(self):
        ct = parse_combined_tag("  v1.2.5_1.0.0  ")
        assert ct.framework == Version(1, 2, 5)

    def test_invalid_no_underscore(self):
        with pytest.raises(ValueError, match="Invalid combined tag"):
            parse_combined_tag("v1.2.5")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="Invalid combined tag"):
            parse_combined_tag("")

    def test_invalid_bad_versions(self):
        with pytest.raises(ValueError, match="Invalid combined tag"):
            parse_combined_tag("v1.2_1.0")

    def test_roundtrip(self):
        """Parse a tag, convert to string, parse again."""
        ct1 = parse_combined_tag("v1.2.5_3.4.5")
        ct2 = parse_combined_tag(str(ct1))
        assert ct2.framework == ct1.framework
        assert ct2.repo == ct1.repo


# ---------------------------------------------------------------------------
# extract_framework_version
# ---------------------------------------------------------------------------

class TestExtractFrameworkVersion:
    def test_basic(self):
        content = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'
        assert extract_framework_version(content) == "1.2.5"

    def test_within_script(self):
        content = """#!/bin/bash
# Some comment
FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.3.0}"

echo "hello"
"""
        assert extract_framework_version(content) == "1.3.0"

    def test_missing_raises(self):
        with pytest.raises(ValueError, match="FRAMEWORK_VERSION not found"):
            extract_framework_version("#!/bin/bash\necho hi")

    def test_empty_content(self):
        with pytest.raises(ValueError, match="FRAMEWORK_VERSION not found"):
            extract_framework_version("")


# ---------------------------------------------------------------------------
# update_framework_version
# ---------------------------------------------------------------------------

class TestUpdateFrameworkVersion:
    def test_basic_update(self):
        content = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'
        result = update_framework_version(content, "1.3.0")
        assert 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.3.0}"' in result
        assert "1.2.5" not in result

    def test_preserves_surrounding_content(self):
        content = """#!/bin/bash
# Comment
FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"
echo "done"
"""
        result = update_framework_version(content, "2.0.0")
        assert "#!/bin/bash" in result
        assert "# Comment" in result
        assert 'echo "done"' in result
        assert "2.0.0" in result
        assert "1.2.5" not in result

    def test_roundtrip(self):
        """Update a version then extract it."""
        original = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'
        updated = update_framework_version(original, "9.8.7")
        extracted = extract_framework_version(updated)
        assert extracted == "9.8.7"
