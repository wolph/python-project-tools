"""Tests for the reader of a workflow's ``on`` key."""

from __future__ import annotations

import pytest

from python_project_tools import workflow_on

# The shape of WoLpH/django-admin-generator, python-statsd and portalocker:
# publish.yml runs on pushed v* tags and has a job called release.
PUSH_TAGS_WITH_RELEASE_JOB: str = (
    'name: Publish to PyPI and GitHub releases\n'
    '\n'
    'on:\n'
    '  push:\n'
    '    tags:\n'
    "      - 'v*'\n"
    '\n'
    'permissions:\n'
    '  contents: read\n'
    '\n'
    'jobs:\n'
    '  publish:\n'
    '    runs-on: ubuntu-latest\n'
    '  # The GitHub release is created here.\n'
    '  release:\n'
    '    needs: publish\n'
    '    runs-on: ubuntu-latest\n'
)

TAG_PUSH_CASES: list[tuple[str, bool]] = [
    # The 2020 pattern: a bare push trigger, the tag check inside the job.
    (
        'on: push\njobs:\n  publish:\n'
        "    if: startsWith(github.ref, 'refs/tags')\n",
        True,
    ),
    ('on: [push, pull_request]\n', True),
    ('on: [pull_request]\n', False),
    ('on: [pull_request,\n  create]\n', True),
    ('on: pull_request  # not push\n', False),
    ('on:\n  push:\n', True),
    ('on:\n  push:\n    paths: [spam]\n', True),
    ('on:\n  push:\n    branches: [master]\n', False),
    ('on:\n  push:\n    branches-ignore: [eggs]\n  pull_request:\n', False),
    ('on:\n  push:\n    branches: [master]\n    tags: ["v*"]\n', True),
    ('on:\n  push:\n    tags-ignore: [spam]\n', True),
    ('on:\n  push:\n    branches:\n    - master\n  release:\n', False),
    ('on:\n  create:\n', True),
    ('on:\n  - push\n  - pull_request\n', True),
    ('on:\n- pull_request\n- create\n', True),
    ('on:\n  pull_request:\n    branches: [master]\n', False),
    ('"on":\n  push:\n    branches:\n      - master\njobs: {}\n', False),
    ('true:\n  push:\n    tags: [v1]\n', True),
    ('on:\n  push: {branches: [master]}\n', False),
    ('on:\n  push: {tags: [v1]}\n', True),
    # A top-level flow map is read loosely: push in it always counts.
    ('on: {push: {branches: [master]}}\n', True),
    ('on: {pull_request: {branches: [master]}}\n', False),
    ('on: {pull_request: {}, create: {}}\n', True),
    ('on:\n  push:  # all\n    branches: [master]\n    # tags: [v1]\n', False),
    (PUSH_TAGS_WITH_RELEASE_JOB, True),
    # No readable on key at all: over-report.
    ('jobs: {}\n', True),
]


@pytest.mark.parametrize(('text', 'expected'), TAG_PUSH_CASES)
def test_fires_on_tag_push(text: str, expected: bool) -> None:
    assert workflow_on.fires_on_tag_push(text) is expected


RELEASE_CASES: list[tuple[str, bool]] = [
    # A job named release is no release event.
    (PUSH_TAGS_WITH_RELEASE_JOB, False),
    (PUSH_TAGS_WITH_RELEASE_JOB.replace('\n', '\r\n'), False),
    ('on: push\njobs:\n  release:\n    runs-on: ubuntu-latest\n', False),
    ('on: [push]\njobs:\n  release: {runs-on: ubuntu-latest}\n', False),
    ('on:\n  push:\n    branches: [release]\n', False),
    ('on: {push: {tags: [release]}}\n', False),
    ('on: [push, release_notes]\n', False),
    ('on:\n  push:\n', False),
    ('on: {push: {branches: [master]}}\n', False),
    ('on:\n  workflow_dispatch:\n# release: later\n', False),
    ('on:\n  workflow_dispatch:\n\nrelease:\n  spam: eggs\n', False),
    # Every shape of a release event.
    ('on: release\n', True),
    ('on: "release"\n', True),
    ('on: release  # publish\n', True),
    ('on: [push, release]\n', True),
    ('on: [release]\n', True),
    ('on: [push,\n  release]\n', True),
    ('on:\n  - push\n  - release\n', True),
    ('on:\n- release\n', True),
    ('on: {release: {types: [published]}}\n', True),
    ('on: {push: {branches: [master]}, release: {}}\n', True),
    ('on:\n  release:\n    types: [published]\n', True),
    ('on:\n  release:\n    types: [created]\n', True),
    ('on:\n  release:\n', True),
    ('on:\n  release: {types: [created]}\n', True),
    (
        'on:\n  push:\n    tags: [v*]\n  release:\n    types: [published]\n',
        True,
    ),
    ('on:\n  "release":\n', True),
    # The key itself, quoted, as YAML 1.1 reads it, after a byte order mark.
    ('"on": {release: null}\n', True),
    ("'on':\n  release:\n", True),
    ('true:\n  release:\n    types: [published]\n', True),
    ('true: [push, release]\n', True),
    ('\ufeffon: release\n', True),
    # No readable on key at all: over-report.
    ('jobs:\n  release:\n    runs-on: ubuntu-latest\n', True),
    ('', True),
]


@pytest.mark.parametrize(('text', 'expected'), RELEASE_CASES)
def test_fires_on_release(text: str, expected: bool) -> None:
    assert workflow_on.fires_on_release(text) is expected


def test_on_events_reads_the_on_value_only() -> None:
    assert workflow_on.on_events(PUSH_TAGS_WITH_RELEASE_JOB) == {
        'push': {'tags'}
    }
    assert workflow_on.on_events('name: spam\njobs: {}\n') is None
