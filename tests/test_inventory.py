import datetime as dt
import json

import pytest

from python_project_tools import cli, github, inventory, pypi


def _file(name: str, yanked: bool = False) -> pypi.PypiFile:
    return pypi.PypiFile(
        filename=name,
        url=f'https://example.invalid/{name}',
        sha256='0' * 64,
        size=1,
        yanked=yanked,
        upload_time=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
        packagetype='sdist' if name.endswith('.tar.gz') else 'bdist_wheel',
    )


def _asset(name: str) -> github.Asset:
    return github.Asset(
        name=name, size=1, url=f'https://example.invalid/{name}'
    )


SDIST: str = 'spam_eggs-1.0.tar.gz'
WHEEL: str = 'spam_eggs-1.0-py3-none-any.whl'


def _patch(
    monkeypatch: pytest.MonkeyPatch, repo: str | None = 'spam/eggs'
) -> None:
    project: pypi.Project = pypi.Project(
        name='spam-eggs',
        latest='1.2',
        releases={
            '1.0': [_file(SDIST), _file(WHEEL)],
            '1.1': [_file('spam_eggs-1.1.tar.gz', yanked=True)],
            '1.2': [],
        },
        repo=repo,
    )
    tags: list[github.Tag] = [
        github.Tag('v1.0', 'a'),
        github.Tag('v1.1', 'b'),
        github.Tag('ci', 'c'),
        github.Tag('v9.9', 'd'),
    ]
    assets: list[github.Asset] = [
        _asset(SDIST),
        _asset(WHEEL),
        _asset(f'{SDIST}.asc'),
        _asset(f'{WHEEL}.asc'),
        _asset(f'{SDIST}.sigstore.json'),
        _asset('SHA256SUMS'),
        _asset('SHA256SUMS.asc'),
    ]
    releases: list[github.Release] = [
        github.Release('v1.0', 1, '', False, False, assets)
    ]
    monkeypatch.setattr(pypi, 'project', lambda package: project)
    monkeypatch.setattr(github, 'tags', lambda repo: tags)
    monkeypatch.setattr(github, 'releases', lambda repo: releases)


def test_inventory_classifies_every_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    inv: inventory.PackageInventory = inventory.inventory('spam-eggs')
    by: dict[str, inventory.VersionRow] = {r.version: r for r in inv.rows}
    assert [r.version for r in inv.rows] == ['1.0', '1.1', '1.2']
    assert by['1.0'].tag == 'v1.0'
    assert by['1.0'].release
    assert by['1.0'].files == 2
    assert by['1.0'].dist_assets == 2
    assert by['1.0'].asc_assets == 2
    assert by['1.0'].sigstore_assets == 1
    assert by['1.0'].sha256sums
    assert by['1.1'].tag == 'v1.1'
    assert not by['1.1'].release
    assert by['1.1'].yanked
    assert by['1.2'].tag is None
    assert by['1.2'].note == 'untagged'
    assert not by['1.2'].yanked
    assert inv.version_tags_without_release == ['v9.9']
    assert inv.odd_tags == ['ci']


def test_empty_on_pypi_note(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    monkeypatch.setattr(github, 'tags', lambda repo: [github.Tag('v1.2', 'e')])
    inv: inventory.PackageInventory = inventory.inventory('spam-eggs')
    by: dict[str, inventory.VersionRow] = {r.version: r for r in inv.rows}
    assert by['1.2'].note == 'empty on pypi'


def test_markdown_has_one_row_per_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    text: str = inventory.to_markdown(inventory.inventory('spam-eggs'))
    lines: list[str] = text.splitlines()
    assert lines[0] == '## spam-eggs (spam/eggs)'
    header: str = (
        '| version | files | yanked | tag | release | dist | asc'
        ' | sigstore | sums | note |'
    )
    assert header in lines
    rows: list[str] = [
        line for line in lines if line.startswith(('| 1.0', '| 1.1', '| 1.2'))
    ]
    assert len(rows) == 3
    assert '| 1.2 | 0 | no | - | no | 0 | 0 | 0 | no | untagged |' in rows
    assert 'Version tags without a PyPI release: v9.9' in lines
    assert 'Other tags: ci' in lines


def test_json_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    inv: inventory.PackageInventory = inventory.inventory('spam-eggs')
    data: list[dict[str, object]] = json.loads(inventory.to_json([inv]))
    assert data[0]['package'] == 'spam-eggs'
    assert data[0]['odd_tags'] == ['ci']
    rows: list[dict[str, object]] = json.loads(inventory.to_json([inv]))[0][
        'rows'
    ]
    assert rows[0]['version'] == '1.0'
    assert rows[0]['sha256sums'] is True


def test_main_exits_2_without_repo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch(monkeypatch, repo=None)
    assert inventory.main(['spam-eggs']) == 2
    err: str = capsys.readouterr().err
    assert (
        'inventory: spam-eggs: no GitHub repository found, pass --repo' in err
    )


def test_main_repo_flag_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch(monkeypatch, repo=None)
    assert inventory.main(['spam-eggs', '--repo', 'spam/eggs', '--json']) == 0
    data: list[dict[str, object]] = json.loads(capsys.readouterr().out)
    assert data[0]['repo'] == 'spam/eggs'


def test_main_prints_markdown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch(monkeypatch)
    assert inventory.main(['spam-eggs']) == 0
    assert '## spam-eggs (spam/eggs)' in capsys.readouterr().out


def test_inventory_is_registered() -> None:
    assert cli.COMMANDS['inventory'] is inventory.main
