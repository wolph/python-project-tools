"""Tests for dispatching and following sign.yml."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest

from python_project_tools import ci_sign, common, signing
from tests.fakes import Recorder, completed

REPO: str = 'WoLpH/spam'
LIST: str = f'gh run list -R {REPO} --workflow sign.yml'


def _runs(*titles: str) -> str:
    return json.dumps(
        [
            {
                'databaseId': 100 + index,
                'displayTitle': title,
                'url': f'https://example.com/runs/{100 + index}',
            }
            for index, title in enumerate(titles)
        ]
    )


class _Clock:
    """Fake time: sleep advances monotonic."""

    def __init__(self) -> None:
        self.now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake: _Clock = _Clock()
    monkeypatch.setattr(ci_sign.time, 'monotonic', fake.monotonic)
    monkeypatch.setattr(ci_sign.time, 'sleep', fake.sleep)
    return fake


def _ids(monkeypatch: pytest.MonkeyPatch, *hexes: str) -> None:
    values: list[uuid.UUID] = [uuid.UUID(hex=item) for item in hexes]
    monkeypatch.setattr(ci_sign.uuid, 'uuid4', lambda: values.pop(0))


OURS: str = 'a' * 32
OTHER: str = 'b' * 32


def test_dispatch_finds_own_run_among_others(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _ids(monkeypatch, OURS)
    rec: Recorder = Recorder(
        {
            f'gh workflow run sign.yml -R {REPO}': completed(),
            LIST: completed(_runs(f'Sign v1.0 {OTHER}', f'Sign v1.0 {OURS}')),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    run: ci_sign.Run = ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')
    assert run == ci_sign.Run(101, 'https://example.com/runs/101')
    assert rec.calls[0] == [
        'gh', 'workflow', 'run', 'sign.yml', '-R', REPO, '--ref', 'master',
        '-f', 'tag=v1.0', '-f', 'package=spam', '-f', f'request_id={OURS}',
    ]  # fmt: skip


def test_two_dispatches_in_flight_find_own_runs(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _ids(monkeypatch, OURS, OTHER)
    both: str = _runs(f'Sign v1.0 {OTHER}', f'Sign v1.0 {OURS}')
    rec: Recorder = Recorder(
        {
            f'gh workflow run sign.yml -R {REPO}': completed(),
            LIST: completed(both),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    first: ci_sign.Run = ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')
    second: ci_sign.Run = ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')
    assert first.id == 101
    assert second.id == 100


def test_dispatch_ignores_prefix_and_substring(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _ids(monkeypatch, OURS)
    longer: str = f'Sign v1.0 x{OURS}'
    rec: Recorder = Recorder(
        {
            f'gh workflow run sign.yml -R {REPO}': completed(),
            LIST: completed(_runs(longer, f'Sign v1.0 {OURS}x')),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(signing.SigningError, match=r'sign\.yml'):
        ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')


def test_dispatch_polls_until_run_appears(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _ids(monkeypatch, OURS)
    replies: list[str] = [_runs(), _runs(f'Sign v1.0 {OURS}')]

    def hook(args: list[str]) -> None:
        rec.replies[LIST] = completed(replies.pop(0))

    rec: Recorder = Recorder(
        {
            f'gh workflow run sign.yml -R {REPO}': completed(),
            LIST: completed(),
        },
        hooks={LIST: hook},
    )
    monkeypatch.setattr(common, 'run', rec)
    run: ci_sign.Run = ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')
    assert run.id == 100
    lists: list[list[str]] = [c for c in rec.calls if c[2:3] == ['list']]
    assert len(lists) == 2


def test_dispatch_gives_up(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _ids(monkeypatch, OURS)
    rec: Recorder = Recorder(
        {
            f'gh workflow run sign.yml -R {REPO}': completed(),
            LIST: completed(_runs()),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(signing.SigningError, match=r'sign\.yml') as info:
        ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')
    assert clock.now >= ci_sign.FIND_SECONDS
    assert str(info.value) == (
        f'sign.yml run for request_id {OURS} did not appear within 120 s. '
        f'Find it at https://github.com/{REPO}/actions/workflows/sign.yml, '
        'and once it is green rerun ppt sign (bundles already made are kept)'
    )


def test_dispatch_list_failure_raises_at_once(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _ids(monkeypatch, OURS)
    rec: Recorder = Recorder(
        {
            f'gh workflow run sign.yml -R {REPO}': completed(),
            LIST: completed(returncode=1, stderr='HTTP 502'),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(signing.SigningError, match='HTTP 502') as info:
        ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')
    assert OURS in str(info.value)
    assert 'already dispatched' in str(info.value)
    assert clock.now == 0


def test_dispatch_list_invalid_json_raises(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _ids(monkeypatch, OURS)
    rec: Recorder = Recorder(
        {
            f'gh workflow run sign.yml -R {REPO}': completed(),
            LIST: completed('not json'),
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(signing.SigningError, match='invalid JSON') as info:
        ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')
    assert OURS in str(info.value)
    assert clock.now == 0


def test_dispatch_failure_raises(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    rec: Recorder = Recorder(
        {'gh workflow run': completed(returncode=1, stderr='no such workflow')}
    )
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(signing.SigningError, match='no such workflow'):
        ci_sign.dispatch(REPO, 'v1.0', 'spam', 'master')


def test_watch_failure_names_url(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({'gh run watch': completed(returncode=1)})
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(signing.SigningError) as info:
        ci_sign.watch(REPO, ci_sign.Run(5, 'https://example.com/r/5'))
    assert str(info.value) == (
        'sign.yml run failed: https://example.com/r/5. Check the run, then '
        'rerun ppt sign to retry (bundles already made are kept)'
    )
    assert rec.captures == [False]
    assert rec.calls[0] == [
        'gh', 'run', 'watch', '5', '-R', REPO, '--exit-status',
    ]  # fmt: skip


def test_watch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({'gh run watch': completed()})
    monkeypatch.setattr(common, 'run', rec)
    ci_sign.watch(REPO, ci_sign.Run(5, 'u'))


def _commit(date: str) -> dict[str, object]:
    return {'commit': {'committer': {'date': date}}}


def test_workflow_since_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({'gh api --paginate': completed('[]')})
    monkeypatch.setattr(common, 'run', rec)
    assert ci_sign.workflow_since(REPO, 'master') is None


def test_workflow_since_404_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder(
        {
            'gh api --paginate': completed(
                returncode=1, stderr='gh: Not Found (HTTP 404)'
            )
        }
    )
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(RuntimeError, match='404'):
        ci_sign.workflow_since(REPO, 'master')


def test_workflow_since_other_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec: Recorder = Recorder(
        {'gh api --paginate': completed(returncode=1, stderr='HTTP 502')}
    )
    monkeypatch.setattr(common, 'run', rec)
    with pytest.raises(RuntimeError):
        ci_sign.workflow_since(REPO, 'master')


def test_workflow_since_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    commits: str = json.dumps(
        [_commit('2026-11-10T08:00:00Z'), _commit('2026-10-01T23:30:00Z')]
    )
    rec: Recorder = Recorder({'gh api --paginate': completed(commits)})
    monkeypatch.setattr(common, 'run', rec)
    assert ci_sign.workflow_since(REPO, 'master') == dt.date(2026, 10, 1)
    assert 'path=.github/workflows/sign.yml' in rec.calls[0][-1]
    assert 'sha=master' in rec.calls[0][-1]


def test_default_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: Recorder = Recorder({f'gh api repos/{REPO}': completed('master\n')})
    monkeypatch.setattr(common, 'run', rec)
    assert ci_sign.default_branch(REPO) == 'master'
