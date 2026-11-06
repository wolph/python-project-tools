import pathlib

import pytest

from python_project_tools import cli, common, verify_release


def test_verify_release_is_registered() -> None:
    assert cli.COMMANDS['verify-release'] is verify_release.main


def test_help_lists_the_skip_groups(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        verify_release.main(['--help'])
    assert exc.value.code == 0
    assert 'pypi,github,stable,docs,install,conda' in capsys.readouterr().out


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
        'pypi,github,stable,docs,install,conda',
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
