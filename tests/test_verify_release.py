import pathlib

import pytest

from python_project_tools import cli, verify_release


def test_verify_release_is_registered() -> None:
    assert cli.COMMANDS['verify-release'] is verify_release.main


def test_help_lists_the_skip_groups(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        verify_release.main(['--help'])
    assert exc.value.code == 0
    assert 'pypi,github,stable,docs,install,conda' in capsys.readouterr().out


def test_package_name_defaults_to_cwd_pyproject(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / 'pyproject.toml').write_text("[project]\nname = 'spam-eggs'\n")
    monkeypatch.chdir(tmp_path)
    assert verify_release.read_package_name() == 'spam-eggs'
