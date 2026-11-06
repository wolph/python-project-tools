"""Tag normalisation and PEP 440 helpers."""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

from python_project_tools.github import Tag

TAG_PREFIXES: tuple[str, ...] = (
    'release-',
    'release_',
    'rel-',
    'version-',
    'v',
)


def normalise(tag_or_version: str) -> str:
    """Lower-case, strip one known prefix, map ``_`` and ``-`` to ``.``."""
    text: str = tag_or_version.lower()
    for prefix in sorted(TAG_PREFIXES, key=len, reverse=True):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.replace('_', '.').replace('-', '.')


def is_version_tag(tag: str) -> bool:
    """Return True when the normalised tag parses as a PEP 440 version."""
    normalised: str = normalise(tag)
    if normalised.startswith('v'):
        # packaging tolerates one leading "v", so "vv1.0" would slip through.
        return False
    try:
        Version(normalised)
    except InvalidVersion:
        return False
    return True


def match_tag(version: str, tags: list[Tag]) -> Tag | None:
    """Find the tag for a version: exact name first, then normalised."""
    for tag in tags:
        if tag.name == version:
            return tag
    wanted: str = normalise(version)
    for tag in tags:
        if normalise(tag.name) == wanted:
            return tag
    return None


def is_prerelease(version: str) -> bool:
    """Return True for PEP 440 pre-releases and dev releases."""
    return Version(version).is_prerelease


def tag_for(version: str) -> str:
    """Return the tag name used for new tags."""
    return f'v{version}'


def sort_key(version: str) -> Version:
    """Return a key that orders versions per PEP 440."""
    return Version(version)
