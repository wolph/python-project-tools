"""Start sign.yml on GitHub and follow its run.

``gh workflow run`` prints no run id, so every dispatch carries a fresh
``request_id`` input. The workflow's ``run-name`` ends with it, which lets
us find our own run among the others, even with two dispatches in flight.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import subprocess
import time
import typing
import urllib.parse
import uuid

from python_project_tools import common, github
from python_project_tools.signing import SigningError

WORKFLOW: str = 'sign.yml'
WORKFLOW_PATH: str = '.github/workflows/sign.yml'
FIND_SECONDS: float = 120
POLL_SECONDS: float = 3


@dataclasses.dataclass(frozen=True)
class Run:
    """A workflow run on GitHub."""

    id: int
    url: str


def default_branch(repo: str) -> str:
    """The default branch of a repository."""
    done: subprocess.CompletedProcess[str] = common.run(
        ['gh', 'api', f'repos/{repo}', '--jq', '.default_branch']
    )
    branch: str = (done.stdout or '').strip()
    if done.returncode != 0 or not branch:
        raise SigningError(
            f'cannot read the default branch of {repo}: '
            f'{(done.stderr or "").strip()}'
        )
    return branch


def workflow_since(repo: str, branch: str) -> dt.date | None:
    """Date of the first commit touching sign.yml on ``branch``.

    None means the commit list is empty, so sign.yml is absent. Every
    failure of gh raises, a 404 included, and is never read as absent.
    """
    path: str = urllib.parse.quote(WORKFLOW_PATH, safe='/')
    sha: str = urllib.parse.quote(branch, safe='')
    commits: list[typing.Any] = typing.cast(
        'list[typing.Any]',
        github._paginated(
            f'repos/{repo}/commits?path={path}&sha={sha}&per_page=100'
        ),
    )
    if not commits:
        return None
    oldest: str = str(commits[-1]['commit']['committer']['date'])
    return dt.datetime.fromisoformat(oldest.replace('Z', '+00:00')).date()


def _dispatched_note(repo: str, request_id: str) -> str:
    return (
        f'{WORKFLOW} was already dispatched in {repo} with request_id '
        f'{request_id}, so do not dispatch again. Look for the run at '
        f'https://github.com/{repo}/actions/workflows/{WORKFLOW}'
    )


def _find_run(repo: str, request_id: str) -> Run | None:
    """The run whose title ends with ``request_id`` as its last word.

    None means no such run is listed yet. A failing gh raises.
    """
    done: subprocess.CompletedProcess[str] = common.run(
        [
            'gh', 'run', 'list', '-R', repo, '--workflow', WORKFLOW,
            '--event', 'workflow_dispatch', '--limit', '20',
            '--json', 'databaseId,displayTitle,url',
        ]
    )  # fmt: skip
    if done.returncode != 0:
        raise SigningError(
            f'gh run list failed: {(done.stderr or "").strip()}. '
            f'{_dispatched_note(repo, request_id)}'
        )
    try:
        raw: list[typing.Any] = json.loads(done.stdout or '[]')
    except json.JSONDecodeError as error:
        raise SigningError(
            f'gh run list printed invalid JSON ({error}). '
            f'{_dispatched_note(repo, request_id)}'
        ) from error
    for item in raw:
        title: str = str(item.get('displayTitle', ''))
        if title.endswith(f' {request_id}'):
            return Run(int(item['databaseId']), str(item['url']))
    return None


def dispatch(repo: str, tag: str, package: str, branch: str) -> Run:
    """Start sign.yml for a tag and return the run it created."""
    request_id: str = uuid.uuid4().hex
    done: subprocess.CompletedProcess[str] = common.run(
        [
            'gh', 'workflow', 'run', WORKFLOW, '-R', repo, '--ref', branch,
            '-f', f'tag={tag}', '-f', f'package={package}',
            '-f', f'request_id={request_id}',
        ]
    )  # fmt: skip
    if done.returncode != 0:
        raise SigningError(
            f'cannot start {WORKFLOW} in {repo}: {(done.stderr or "").strip()}'
        )
    deadline: float = time.monotonic() + FIND_SECONDS
    while True:
        run: Run | None = _find_run(repo, request_id)
        if run is not None:
            return run
        if time.monotonic() >= deadline:
            raise SigningError(
                f'its run did not appear within {FIND_SECONDS:g} seconds. '
                f'{_dispatched_note(repo, request_id)}'
            )
        time.sleep(POLL_SECONDS)


def watch(repo: str, run: Run) -> None:
    """Follow a run to its end, raising when it fails."""
    done: subprocess.CompletedProcess[str] = common.run(
        ['gh', 'run', 'watch', str(run.id), '-R', repo, '--exit-status'],
        capture=False,
    )
    if done.returncode != 0:
        raise SigningError(f'{WORKFLOW} run failed: {run.url}')
