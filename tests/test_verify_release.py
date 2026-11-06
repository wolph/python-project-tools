import pathlib
import subprocess

import pytest

from python_project_tools import (
    cli,
    common,
    github as gh,
    verify_release,
)


def test_verify_release_is_registered() -> None:
    assert cli.COMMANDS['verify-release'] is verify_release.main


def test_help_lists_the_skip_groups(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        verify_release.main(['--help'])
    assert exc.value.code == 0
    assert (
        'pypi,github,stable,docs,install,conda,signatures'
        in capsys.readouterr().out
    )


def test_package_flag_works_without_pyproject(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    passed: common.Result = common.Result('spam', 'PASS', 'eggs')
    monkeypatch.setattr(verify_release, 'check_tag', lambda tag: passed)
    monkeypatch.setattr(verify_release, 'check_version', lambda v, t: passed)
    monkeypatch.setattr(verify_release, 'check_changelog', lambda v, t: passed)
    argv: list[str] = [
        '--version',
        '1.0',
        '--package',
        'spam',
        '--skip',
        'pypi,github,stable,docs,install,conda,signatures',
    ]
    assert verify_release.main(argv) == 0


def test_poll_annotation_resolves() -> None:
    import typing

    hints: dict[str, object] = typing.get_type_hints(verify_release.poll)
    assert 'probe' in hints


def test_conda_candidates_from_package_name() -> None:
    names: list[str] = verify_release.conda_candidates('Spam_Eggs.Ham', None)
    assert names == ['spam_eggs.ham', 'spam-eggs.ham', 'spameggsham']


def test_conda_candidates_deduplicates() -> None:
    assert verify_release.conda_candidates('spam', None) == ['spam']


def test_conda_candidates_prefers_conda_name() -> None:
    assert verify_release.conda_candidates('spam_eggs', 'eggs') == ['eggs']


def test_check_conda_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        common,
        'fetch_json',
        lambda url: {'versions': ['1.0.0', '1.1.0']},
    )
    result: common.Result = verify_release.check_conda('spam', '1.1.0', None)
    assert result.status == 'PASS'
    assert '1.1.0' in result.evidence


def test_check_conda_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        common, 'fetch_json', lambda url: {'versions': ['1.0.0']}
    )
    result: common.Result = verify_release.check_conda('spam', '1.1.0', None)
    assert result.status == 'FAIL'
    assert 'latest is 1.0.0' in result.evidence


def test_check_conda_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, 'fetch_json', lambda url: None)
    result: common.Result = verify_release.check_conda('spam', '1.1.0', None)
    assert result.status == 'SKIP'


def test_check_conda_fails_on_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(url: str) -> object:
        raise common.FetchError(f'{url}: HTTP 503')

    monkeypatch.setattr(common, 'fetch_json', boom)
    result: common.Result = verify_release.check_conda('spam', '1.1.0', None)
    assert result.status == 'FAIL'
    assert 'HTTP 503' in result.evidence


def test_check_pypi_fails_on_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(url: str) -> object:
        raise common.FetchError(f'{url}: HTTP 503')

    monkeypatch.setattr(common, 'fetch_json', boom)
    result: common.Result = verify_release.check_pypi('spam', '1.1.0')
    assert result.status == 'FAIL'
    assert 'HTTP 503' in result.evidence


FULL_SET: list[str] = [
    's.whl',
    's.whl.asc',
    's.whl.sigstore.json',
    's.tar.gz',
    's.tar.gz.asc',
    's.tar.gz.sigstore.json',
    'SHA256SUMS',
    'SHA256SUMS.asc',
]


def _patch_release(
    monkeypatch: pytest.MonkeyPatch, names: list[str] | None
) -> None:
    repo: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        [], 0, 'spam/eggs\n', ''
    )
    monkeypatch.setattr(common, 'run', lambda *a, **k: repo)
    found: list[gh.Release] = []
    if names is not None:
        found.append(
            gh.Release(
                tag='v1.0',
                id=1,
                body='',
                prerelease=False,
                draft=False,
                assets=[
                    gh.Asset(name, 1, f'https://example.com/{name}')
                    for name in names
                ],
            )
        )
    monkeypatch.setattr(gh, 'releases', lambda repo: found)


def test_check_signatures_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_release(monkeypatch, FULL_SET)
    res: common.Result = verify_release.check_signatures('v1.0')
    assert res.status == 'PASS'


def test_check_signatures_lists_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names: list[str] = [
        n for n in FULL_SET if n not in ('s.whl.sigstore.json', 'SHA256SUMS')
    ]
    _patch_release(monkeypatch, names)
    res: common.Result = verify_release.check_signatures('v1.0')
    assert res.status == 'FAIL'
    assert 's.whl.sigstore.json' in res.evidence
    assert 'SHA256SUMS' in res.evidence


def test_check_signatures_no_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_release(monkeypatch, None)
    res: common.Result = verify_release.check_signatures('v1.0')
    assert res.status == 'FAIL'
    assert res.evidence == 'no release for v1.0'


def test_signatures_skip_group(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def fake(tag: str) -> common.Result:
        called.append(tag)
        return common.Result('signatures', 'PASS', '')

    monkeypatch.setattr(verify_release, 'check_signatures', fake)
    passed: common.Result = common.Result('spam', 'PASS', 'eggs')
    monkeypatch.setattr(verify_release, 'check_tag', lambda tag: passed)
    monkeypatch.setattr(verify_release, 'check_version', lambda v, t: passed)
    monkeypatch.setattr(verify_release, 'check_changelog', lambda v, t: passed)
    base: list[str] = ['--version', '1.0', '--package', 'spam']
    rest: str = 'pypi,github,stable,docs,install,conda'
    verify_release.main([*base, '--skip', rest + ',signatures'])
    assert called == []
    verify_release.main([*base, '--skip', rest])
    assert called == ['v1.0']
