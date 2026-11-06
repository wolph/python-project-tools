"""Tests for ppt backfill --batch: several packages, one Sigstore login."""

from __future__ import annotations

import pathlib

import pytest

from python_project_tools import (
    backfill,
    backfill_batch,
    common,
    github,
    pypi,
    sign,
    signing,
)
from tests.test_backfill import (
    SHA,
    Signer,
    World,
    install,
    pypi_file,
    release,
)

# Before the run, after the first release, before marking, after the run.
EMPTY_REPO_LATEST: list[str | None] = [None, 'v1.0', 'v1.0', 'v1.0']


class Batch:
    """Several packages, each with its own repository, checkout and Latest."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        self.tmp_path: pathlib.Path = tmp_path
        self.world: World = install(monkeypatch, World(), fake_sign=False)
        self.signer: Signer = Signer(monkeypatch)
        self.projects: dict[str, pypi.Project] = {}
        self.tags: dict[str, list[github.Tag]] = {}
        self.latest: dict[str, list[str | None]] = {}
        self.meta: dict[str, object] = {}
        self.finished: list[tuple[str, str]] = []
        self.sigstore_events: list[int] = []
        monkeypatch.setattr(pypi, 'project', self.project)
        monkeypatch.setattr(github, 'tags', lambda repo: self.tags[repo])
        monkeypatch.setattr(github, 'releases', lambda repo: [])
        monkeypatch.setattr(github, 'latest_release', self.latest_release)
        monkeypatch.setattr(common, 'gh_json', self.gh_json)
        original = sign.finish_pending

        def finish_pending(
            release: github.Release,
            outcome: sign.SignOutcome,
            rebuilt_names: list[str],
            options: sign.SignOptions,
        ) -> sign.SignOutcome:
            self.finished.append((options.repo, release.tag))
            return original(release, outcome, rebuilt_names, options)

        monkeypatch.setattr(sign, 'finish_pending', finish_pending)
        sigstore = signing.sigstore_sign

        def sigstore_sign(paths: list[pathlib.Path]) -> list[pathlib.Path]:
            self.sigstore_events.append(len(self.world.created))
            return sigstore(paths)

        monkeypatch.setattr(signing, 'sigstore_sign', sigstore_sign)

    def project(self, package: str) -> pypi.Project:
        found: pypi.Project | None = self.projects.get(package)
        if found is None:
            raise LookupError(f'{package} is not on PyPI')
        return found

    def latest_release(self, repo: str) -> github.Release | None:
        tag: str | None = self.latest[repo].pop(0)
        return None if tag is None else release(tag)

    def gh_json(self, args: list[str]) -> object | None:
        return self.meta[args[1].removeprefix('repos/')]

    def add(
        self,
        package: str,
        latest: list[str | None],
        prefix: str | None = None,
    ) -> pathlib.Path:
        """A package with version 1.0 and its checkout directory."""
        repo: str = f'WoLpH/{package}'
        stem: str = prefix or package
        self.projects[package] = pypi.Project(
            name=package,
            latest='1.0',
            releases={
                '1.0': [
                    pypi_file(f'{stem}-1.0.tar.gz'),
                    pypi_file(
                        f'{stem}-1.0-py3-none-any.whl', kind='bdist_wheel'
                    ),
                ]
            },
            repo=repo,
        )
        self.tags[repo] = [github.Tag('v1.0', SHA)]
        self.latest[repo] = list(latest)
        self.meta[repo] = {'archived': False}
        checkout: pathlib.Path = self.tmp_path / 'checkouts' / package
        checkout.mkdir(parents=True)
        return checkout

    def file(self, *packages: str) -> pathlib.Path:
        """A batch file listing these packages with a comment and a gap."""
        lines: list[str] = ['# packages to backfill', '']
        lines.extend(
            f'{name} {self.tmp_path / "checkouts" / name}' for name in packages
        )
        path: pathlib.Path = self.tmp_path / 'batch.txt'
        path.write_text('\n'.join(lines) + '\n')
        return path

    def bundles_signed(self) -> list[str]:
        return sorted(
            path.name for call in self.signer.sigstore_calls for path in call
        )


def files_of_both(*stems: str) -> list[str]:
    return sorted(
        name
        for stem in stems
        for name in (f'{stem}-1.0.tar.gz', f'{stem}-1.0-py3-none-any.whl')
    )


def test_a_batch_signs_every_package_in_one_sigstore_invocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    batch.add('spam', EMPTY_REPO_LATEST)
    batch.add('eggs', EMPTY_REPO_LATEST)
    status: int = backfill_batch.main(
        ['--batch', str(batch.file('spam', 'eggs'))]
    )
    assert status == 0
    assert len(batch.signer.sigstore_calls) == 1
    assert batch.bundles_signed() == files_of_both('spam', 'eggs')
    # The one invocation comes after both packages created their release.
    assert batch.sigstore_events == [2]
    assert batch.finished == [('WoLpH/spam', 'v1.0'), ('WoLpH/eggs', 'v1.0')]
    out: str = capsys.readouterr().out
    spam_at: int = out.index('## spam (WoLpH/spam)')
    eggs_at: int = out.index('## eggs (WoLpH/eggs)')
    summary_at: int = out.index('| package | exit | rows signed |')
    assert spam_at < eggs_at < summary_at
    assert out.count('Latest before: none, after: v1.0, target: v1.0') == 2
    summary: str = out[summary_at:]
    latest: str = 'before: none, after: v1.0, target: v1.0, OK'
    assert f'| spam | 0 | 1 | 0 | {latest} |' in summary
    assert f'| eggs | 0 | 1 | 0 | {latest} |' in summary
    assert batch.latest == {'WoLpH/spam': [], 'WoLpH/eggs': []}


def test_a_package_refused_in_preflight_does_not_stop_the_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    batch.add('spam', [])
    batch.add('eggs', EMPTY_REPO_LATEST)
    batch.meta['WoLpH/spam'] = {'archived': True}
    status: int = backfill_batch.main(
        ['--batch', str(batch.file('spam', 'eggs'))]
    )
    assert status == 2
    assert [created[0] for created in batch.world.created] == ['v1.0']
    assert len(batch.signer.sigstore_calls) == 1
    assert batch.bundles_signed() == files_of_both('eggs')
    assert batch.finished == [('WoLpH/eggs', 'v1.0')]
    captured = capsys.readouterr()
    reason: str = 'WoLpH/spam is archived, run: gh repo unarchive WoLpH/spam'
    assert f'spam: {reason}' in captured.err
    assert f'| spam | 2 | 0 | 0 | {reason} |' in captured.out
    assert '| eggs | 0 | 1 | 0 |' in captured.out


def test_an_unknown_package_is_refused_and_the_batch_goes_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    batch.add('eggs', EMPTY_REPO_LATEST)
    (tmp_path / 'checkouts' / 'ham').mkdir()
    status: int = backfill_batch.main(
        ['--batch', str(batch.file('ham', 'eggs'))]
    )
    assert status == 2
    assert batch.finished == [('WoLpH/eggs', 'v1.0')]
    assert 'ham: ham is not on PyPI' in capsys.readouterr().err


def test_a_stopped_package_still_gets_its_bundles_in_the_shared_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    batch.add('spam', ['v0.9', 'v0.9'])
    batch.projects['spam'].releases['1.1'] = [pypi_file('spam-1.1.tar.gz')]
    batch.tags['WoLpH/spam'].append(github.Tag('v1.1', SHA))
    batch.add('eggs', EMPTY_REPO_LATEST)
    create = github.create_release

    def create_release(
        repo: str, tag: str, title: str, notes: str, prerelease: bool
    ) -> github.Release:
        if (repo, tag) == ('WoLpH/spam', 'v1.1'):
            raise backfill.LatestMarkedError('simulated stop at v1.1')
        return create(repo, tag, title, notes, prerelease)

    monkeypatch.setattr(github, 'create_release', create_release)
    status: int = backfill_batch.main(
        ['--batch', str(batch.file('spam', 'eggs'))]
    )
    assert status == 2
    assert [created[0] for created in batch.world.created] == ['v1.0', 'v1.0']
    assert len(batch.signer.sigstore_calls) == 1
    assert batch.bundles_signed() == files_of_both('spam', 'eggs')
    assert batch.finished == [('WoLpH/spam', 'v1.0'), ('WoLpH/eggs', 'v1.0')]
    captured = capsys.readouterr()
    assert 'spam: simulated stop at v1.1' in captured.err
    assert 'Stopped: simulated stop at v1.1' in captured.out
    assert '| 1.0 | v1.0 | exists | create | pypi | signed |' in captured.out
    assert '| spam | 2 | 1 | 1 | stopped, before: v0.9' in captured.out
    assert '| eggs | 0 | 1 | 0 |' in captured.out


def test_same_file_names_in_two_packages_get_their_own_bundles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    batch.add('spam', EMPTY_REPO_LATEST, prefix='shared')
    batch.add('eggs', EMPTY_REPO_LATEST, prefix='shared')
    status: int = backfill_batch.main(
        ['--batch', str(batch.file('spam', 'eggs'))]
    )
    assert status == 0
    # Each bundle is written next to its file, so equal names share the
    # one signer run without overwriting each other.
    assert len(batch.signer.sigstore_calls) == 1
    signed: list[pathlib.Path] = [
        path for call in batch.signer.sigstore_calls for path in call
    ]
    assert len(set(signed)) == 4
    assert batch.finished == [('WoLpH/spam', 'v1.0'), ('WoLpH/eggs', 'v1.0')]
    assert len(batch.signer.bundle_uploads()) == 2


@pytest.mark.parametrize(
    ('extra', 'message'),
    [
        (['--only', '1.0'], '--batch cannot be combined with --only'),
        (['--repo', 'WoLpH/spam'], '--batch cannot be combined with --repo'),
        (['--checkout', '.'], '--batch cannot be combined with --checkout'),
        (['spam'], '--batch takes no PACKAGE'),
    ],
)
def test_batch_refuses_options_that_name_one_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    message: str,
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    batch.add('spam', EMPTY_REPO_LATEST)
    path: pathlib.Path = batch.file('spam')
    assert backfill_batch.main(['--batch', str(path), *extra]) == 2
    assert message in capsys.readouterr().err
    assert not batch.world.created
    assert not batch.world.rec.calls


def test_no_package_and_no_batch_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert backfill_batch.main([]) == 2
    assert 'give a PACKAGE or --batch FILE' in capsys.readouterr().err


def test_the_single_package_form_prints_no_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    checkout: pathlib.Path = batch.add('spam', EMPTY_REPO_LATEST)
    assert backfill_batch.main(['spam', '--checkout', str(checkout)]) == 0
    out: str = capsys.readouterr().out
    assert '| package | exit |' not in out
    assert '## spam' not in out
    assert len(batch.signer.sigstore_calls) == 1


def test_parse_batch_reads_pairs_and_skips_comments(
    tmp_path: pathlib.Path,
) -> None:
    spaced: pathlib.Path = tmp_path / 'with space'
    spaced.mkdir()
    text: str = (
        f'# a comment\n\n  spam   {tmp_path}  \n  # indented comment\n'
        f'eggs {spaced}\n'
    )
    assert backfill_batch.parse_batch(text) == [
        backfill_batch.BatchEntry('spam', tmp_path),
        backfill_batch.BatchEntry('eggs', spaced),
    ]


def test_parse_batch_expands_the_home_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # expanduser reads HOME on POSIX and USERPROFILE on Windows
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    (tmp_path / 'spam').mkdir()
    assert backfill_batch.parse_batch('spam ~/spam\n') == [
        backfill_batch.BatchEntry('spam', tmp_path / 'spam')
    ]


@pytest.mark.parametrize(
    ('text', 'message'),
    [
        ('spam\n', 'line 1: expected PACKAGE CHECKOUT_PATH'),
        ('spam relative/path\n', 'line 1: checkout .* is not an absolute'),
        ('spam {missing}\n', 'line 1: checkout .* is not a directory'),
        ('spam {tmp}\nSpam {tmp}\n', 'line 2: Spam is already on line 1'),
        ('# only a comment\n\n', 'lists no packages'),
    ],
)
def test_parse_batch_refuses_a_bad_file(
    tmp_path: pathlib.Path, text: str, message: str
) -> None:
    filled: str = text.format(tmp=tmp_path, missing=tmp_path / 'missing')
    with pytest.raises(backfill_batch.BatchFileError, match=message):
        backfill_batch.parse_batch(filled)


def test_a_bad_batch_file_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    batch.add('spam', EMPTY_REPO_LATEST)
    path: pathlib.Path = batch.file('spam')
    path.write_text(path.read_text() + 'eggs\n')
    assert backfill_batch.main(['--batch', str(path)]) == 2
    assert 'expected PACKAGE CHECKOUT_PATH' in capsys.readouterr().err
    assert not batch.world.rec.calls
    assert not batch.world.created


def test_a_missing_batch_file_exits_2(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing: pathlib.Path = tmp_path / 'missing.txt'
    assert backfill_batch.main(['--batch', str(missing)]) == 2
    assert 'cannot read' in capsys.readouterr().err


def test_the_worst_package_sets_the_batch_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    batch: Batch = Batch(monkeypatch, tmp_path)
    batch.add('spam', EMPTY_REPO_LATEST)
    # Latest ends up elsewhere, so eggs reports CHANGED and exits 1.
    batch.add('eggs', [None, 'v1.0', 'v1.0', 'v0.1'])
    status: int = backfill_batch.main(
        ['--batch', str(batch.file('spam', 'eggs'))]
    )
    assert status == 1
