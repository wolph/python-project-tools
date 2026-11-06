import subprocess
import sys

import pytest

from python_project_tools import __version__, cli


def test_version_matches_metadata() -> None:
    import importlib.metadata

    assert __version__ == importlib.metadata.version('python-project-tools')


def test_module_entry_prints_version() -> None:
    done: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-m', 'python_project_tools', '--version'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert done.stdout.strip() == f'ppt {__version__}'


@pytest.mark.parametrize('argv', [[], ['nonsense']])
def test_unknown_or_missing_command_exits_2(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert 'usage: ppt' in capsys.readouterr().err


def test_registered_command_receives_remaining_argv() -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> int:
        seen.append(argv)
        return 7

    cli.COMMANDS['fake'] = fake
    try:
        assert cli.main(['fake', '--x', '1']) == 7
    finally:
        del cli.COMMANDS['fake']
    assert seen == [['--x', '1']]
