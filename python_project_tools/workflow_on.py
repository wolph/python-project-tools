"""Read the ``on`` key of a GitHub Actions workflow without a YAML parser.

The events decide whether pushing a tag or creating a release runs the
workflow. Only the top-level ``on`` value counts: a job that happens to be
called ``release`` triggers nothing. YAML 1.1 reads a bare ``on`` as the
boolean true, so a ``true:`` key is read as ``on`` too. A file whose ``on``
key cannot be found counts as firing, since being wrong here publishes a
release.
"""

from __future__ import annotations

import re
import typing

# The top-level ``on`` key of a workflow and its inline value.
ON_LINE: re.Pattern[str] = re.compile(
    r"""^(?:on|"on"|'on'|true):(?:\s+(.*))?$"""
)
# One ``key: value`` line of a block map, key optionally quoted.
KEY_LINE: re.Pattern[str] = re.compile(
    r"""^["']?([\w-]+)["']?:(?:\s+(.*))?$"""
)
FLOW_KEY: re.Pattern[str] = re.compile(r'([\w-]+)["\']?\s*:')
WORD: re.Pattern[str] = re.compile(r'[\w-]+')
COMMENT: re.Pattern[str] = re.compile(r'(?:^|\s)#.*$')
BYTE_ORDER_MARK: str = '\ufeff'
TAG_FILTERS: frozenset[str] = frozenset({'tags', 'tags-ignore'})
BRANCH_FILTERS: frozenset[str] = frozenset({'branches', 'branches-ignore'})
RELEASE_EVENT: str = 'release'
# Event name to the keys of its map: None when the value is no block map
# (a list entry, a scalar or a flow value read loosely).
Events: typing.TypeAlias = dict[str, set[str] | None]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' '))


def _clean_lines(text: str) -> list[str]:
    """Lines without comments and trailing blanks, blank lines dropped."""
    lines: list[str] = [
        COMMENT.sub('', line).rstrip()
        for line in text.removeprefix(BYTE_ORDER_MARK).split('\n')
    ]
    return [line for line in lines if line.strip()]


def _on_value(lines: list[str]) -> tuple[str, list[str]] | None:
    """The inline value of the top-level ``on`` key and the lines below it.

    A block sequence may sit at the same indent as ``on`` itself, so lines
    starting with ``-`` belong to it too.
    """
    for index, line in enumerate(lines):
        found: re.Match[str] | None = ON_LINE.match(line)
        if found is None:
            continue
        body: list[str] = []
        for other in lines[index + 1 :]:
            if _indent(other) == 0 and not other.startswith('-'):
                break
            body.append(other)
        return (found.group(1) or '').strip(), body
    return None


def _block_keys(body: list[str]) -> dict[str, tuple[str, list[str]]]:
    """Keys at the first indent of a block, each with value and nested lines.

    A list entry (``- push``) becomes a key with no value.
    """
    keys: dict[str, tuple[str, list[str]]] = {}
    if not body:
        return keys
    level: int = _indent(body[0])
    current: list[str] | None = None
    for line in body:
        if _indent(line) > level:
            if current is not None:
                current.append(line)
            continue
        item: str = line.strip()
        if item.startswith('-'):
            item = item[1:].strip().strip('"\'')
            keys[item] = ('', [])
            current = None
            continue
        found: re.Match[str] | None = KEY_LINE.match(item)
        current = None
        if found is not None:
            current = []
            keys[found.group(1)] = ((found.group(2) or '').strip(), current)
    return keys


def _value_keys(inline: str, nested: list[str]) -> set[str] | None:
    """The keys of an event's map, or None when it has no map."""
    if inline.startswith('{'):
        return set(FLOW_KEY.findall(inline))
    if inline:
        return None
    return set(_block_keys(nested))


def on_events(text: str) -> Events | None:
    """The events of a workflow's ``on`` key, or None when there is none.

    Reads the shapes GitHub documents: ``on: push``, ``on: [a, b]``, a
    block map or block list under ``on:``, and a flow map read loosely.
    """
    value: tuple[str, list[str]] | None = _on_value(_clean_lines(text))
    if value is None:
        return None
    inline, body = value
    if inline.startswith(('[', '{')):
        flow: str = ' '.join([inline, *(line.strip() for line in body)])
        if inline.startswith('['):
            return dict.fromkeys(WORD.findall(flow))
        # Nested keys land here too, over-reporting the event set.
        return dict.fromkeys(FLOW_KEY.findall(flow))
    if inline:
        return {inline.strip('"\''): None}
    return {
        name: _value_keys(event_inline, nested)
        for name, (event_inline, nested) in _block_keys(body).items()
    }


def fires_on_tag_push(text: str) -> bool:
    """Whether a workflow runs when a tag is pushed.

    ``create`` always fires. ``push`` fires unless it filters on branches
    without also filtering on tags. A file whose ``on`` key cannot be found
    counts as firing.
    """
    events: Events | None = on_events(text)
    if events is None:
        return True
    if 'create' in events:
        return True
    if 'push' not in events:
        return False
    keys: set[str] | None = events['push']
    if keys is None or keys & TAG_FILTERS:
        return True
    return not keys & BRANCH_FILTERS


def fires_on_release(text: str) -> bool:
    """Whether a workflow runs on a release event.

    Only ``release`` as an event of the ``on`` value counts, in any shape
    ``on_events`` reads. A file whose ``on`` key cannot be found counts as
    firing.
    """
    events: Events | None = on_events(text)
    return events is None or RELEASE_EVENT in events
