"""Tests for ppt sign."""

from __future__ import annotations

import datetime as dt
import pathlib
import typing

import pytest

from python_project_tools import cli, common, github, pypi, sign, signing
from tests.fakes import Recorder

WHL_BYTES: bytes = b'wheel bytes'
SDIST_BYTES: bytes = b'sdist bytes'
DISTS: tuple[str, str] = ('s.whl', 's.tar.gz')
ALL_NAMES: list[str] = [
    's.whl',
    's.whl.asc',
    's.whl.sigstore.json',
    's.tar.gz',
    's.tar.gz.asc',
    's.tar.gz.sigstore.json',
    'SHA256SUMS',
    'SHA256SUMS.asc',
]


class Upload(typing.NamedTuple):
    names: list[str]
    clobber: bool


class World:
    """Replacements for the outside world, with what they were asked."""

    def __init__(self, asset_bytes: dict[str, bytes]) -> None:
        self.asset_bytes: dict[str, bytes] = asset_bytes
        self.uploads: list[Upload] = []
        self.edits: list[str] = []
        self.downloaded: list[str] = []
        self.signed: list[str] = []
        self.sigstore_calls: int = 0

    @property
    def uploaded(self) -> list[str]:
        return [name for up in self.uploads for name in up.names]


def pypi_file(name: str, data: bytes) -> pypi.PypiFile:
    import hashlib

    return pypi.PypiFile(
        filename=name,
        url=f'https://files.example/{name}',
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        yanked=False,
        upload_time=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
        packagetype='sdist' if name.endswith('.gz') else 'bdist_wheel',
    )


def release(
    names: list[str], body: str = '', tag: str = 'v1.0'
) -> github.Release:
    return github.Release(
        tag=tag,
        id=1,
        body=body,
        prerelease=False,
        draft=False,
        assets=[
            github.Asset(n, 1, f'https://assets.example/{n}') for n in names
        ],
    )


@pytest.fixture
def files() -> list[pypi.PypiFile]:
    return [pypi_file('s.whl', WHL_BYTES), pypi_file('s.tar.gz', SDIST_BYTES)]


@pytest.fixture
def options() -> sign.SignOptions:
    return sign.SignOptions(
        repo='o/r',
        package='s',
        signed_on=dt.date(2026, 11, 6),
    )


def install(
    monkeypatch: pytest.MonkeyPatch,
    asset_bytes: dict[str, bytes] | None = None,
) -> tuple[World, Recorder]:
    world: World = World(asset_bytes or {})
    rec: Recorder = Recorder({})
    monkeypatch.setattr(common, 'run', rec)
    data: dict[str, bytes] = {'s.whl': WHL_BYTES, 's.tar.gz': SDIST_BYTES}

    def download(file: pypi.PypiFile, into: pathlib.Path) -> pathlib.Path:
        into.mkdir(parents=True, exist_ok=True)
        target: pathlib.Path = into / file.filename
        target.write_bytes(data[file.filename])
        return target

    def gpg_sign(path: pathlib.Path, key: str) -> pathlib.Path:
        out: pathlib.Path = path.with_name(path.name + '.asc')
        out.write_text(f'sig {key}')
        world.signed.append(path.name)
        return out

    def sigstore_sign(paths: list[pathlib.Path]) -> list[pathlib.Path]:
        world.sigstore_calls += 1
        bundles: list[pathlib.Path] = [
            p.with_name(p.name + '.sigstore.json') for p in paths
        ]
        for bundle in bundles:
            bundle.write_text('bundle')
        return bundles

    def download_asset(
        asset: github.Asset, into: pathlib.Path
    ) -> pathlib.Path:
        into.mkdir(parents=True, exist_ok=True)
        world.downloaded.append(asset.name)
        target: pathlib.Path = into / asset.name
        target.write_bytes(world.asset_bytes[asset.name])
        return target

    def upload_assets(
        repo: str,
        tag: str,
        paths: list[pathlib.Path],
        clobber: bool = False,
    ) -> None:
        world.uploads.append(Upload([p.name for p in paths], clobber))

    def edit_notes(repo: str, tag: str, body: str) -> None:
        world.edits.append(body)

    monkeypatch.setattr(pypi, 'download', download)
    monkeypatch.setattr(signing, 'gpg_sign', gpg_sign)
    monkeypatch.setattr(signing, 'sigstore_sign', sigstore_sign)
    monkeypatch.setattr(github, 'download_asset', download_asset)
    monkeypatch.setattr(github, 'upload_assets', upload_assets)
    monkeypatch.setattr(github, 'edit_notes', edit_notes)
    return world, rec


def test_sign_release_uploads_everything_on_a_bare_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    world, _ = install(monkeypatch)
    out: sign.SignOutcome = sign.sign_release(
        release([]), files, tmp_path, options
    )
    assert sorted(out.uploaded) == sorted(ALL_NAMES)
    assert out.status == 'signed'
    assert sorted(world.uploaded) == sorted(ALL_NAMES)
    assert world.sigstore_calls == 1
    assert len(world.edits) == 1
    assert world.edits[0].startswith(sign.FOOTER_MARKER)


def test_sign_release_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    world, rec = install(
        monkeypatch, {'s.whl': WHL_BYTES, 's.tar.gz': SDIST_BYTES}
    )
    signed: github.Release = release(
        ALL_NAMES, body=f'notes\n\n{sign.FOOTER_MARKER}\nSigned'
    )
    out: sign.SignOutcome = sign.sign_release(signed, files, tmp_path, options)
    assert out.uploaded == []
    assert out.status == 'already signed'
    assert sorted(out.kept) == sorted(ALL_NAMES)
    assert world.uploads == []
    assert world.edits == []
    assert not any('release edit' in ' '.join(c) for c in rec.calls)
    # Dist files are compared by content, signatures are not downloaded.
    assert sorted(world.downloaded) == sorted(DISTS)
    assert world.sigstore_calls == 0
    assert world.signed == []


def test_conflicting_asset_is_left_alone_and_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    world, _ = install(monkeypatch, {'s.whl': b'other'})
    out: sign.SignOutcome = sign.sign_release(
        release(['s.whl']), files[:1], tmp_path, options
    )
    assert out.conflicts == ['s.whl']
    assert out.status == 'failed'
    assert out.uploaded == []
    assert out.reason == 'conflicting assets left alone: s.whl'
    assert world.uploads == []
    assert world.edits == []
    assert world.signed == []
    assert world.sigstore_calls == 0


def test_replace_assets_clobbers_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    world, _ = install(monkeypatch, {'s.whl': b'other'})
    options.replace_assets = True
    out: sign.SignOutcome = sign.sign_release(
        release(['s.whl']), files[:1], tmp_path, options
    )
    assert out.conflicts == ['s.whl']
    assert 's.whl' in out.uploaded
    clobbered: list[Upload] = [u for u in world.uploads if u.clobber]
    assert len(clobbered) == 1
    assert clobbered[0].names == ['s.whl']


def test_gpg_only_skips_sigstore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    world, _ = install(monkeypatch)
    options.sigstore = False
    out: sign.SignOutcome = sign.sign_release(
        release([]), files, tmp_path, options
    )
    assert world.sigstore_calls == 0
    assert not any(n.endswith('.sigstore.json') for n in out.uploaded)
    assert 's.whl.asc' in out.uploaded
    assert 'SHA256SUMS.asc' in out.uploaded


def test_sigstore_only_skips_gpg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    world, _ = install(monkeypatch)
    options.gpg = False
    out: sign.SignOutcome = sign.sign_release(
        release([]), files, tmp_path, options
    )
    assert world.signed == []
    assert not any(n.endswith('.asc') for n in out.uploaded)
    assert 'SHA256SUMS' in out.uploaded


def test_footer_names_rebuilt_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    options: sign.SignOptions,
) -> None:
    world, _ = install(monkeypatch)
    local_dir: pathlib.Path = tmp_path / 'local'
    local_dir.mkdir()
    local: pathlib.Path = local_dir / 's.whl'
    local.write_bytes(WHL_BYTES)
    options.rebuilt = {'s.whl': '# rebuilt from tag'}
    out: sign.SignOutcome = sign.sign_release(
        release([], body='Release notes'),
        [local],
        tmp_path / 'work',
        options,
    )
    assert out.status == 'signed'
    assert len(world.edits) == 1
    body: str = world.edits[0]
    assert body.startswith('Release notes\n\n' + sign.FOOTER_MARKER)
    assert 'Rebuilt files, if any: s.whl.' in body
    assert 'Signed on 2026-11-06' in body
    assert signing.DEFAULT_GPG_KEY in body
    assert sign.VERIFY_URL in body
    assert 'Rebuilt files, if any: none.' in sign.footer(options, [])


def test_dry_run_performs_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, rec = install(monkeypatch)
    options.dry_run = True
    out: sign.SignOutcome = sign.sign_release(
        release([]), files, tmp_path, options
    )
    assert rec.calls == []
    assert world.uploads == []
    assert world.edits == []
    assert world.downloaded == []
    assert list(tmp_path.iterdir()) == []
    printed: str = capsys.readouterr().out
    assert 'would download s.whl' in printed
    assert 'would upload SHA256SUMS' in printed
    assert 'would append footer' in printed
    assert out.status == 'signed'


def test_integrity_error_fails_the_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    world, _ = install(monkeypatch)

    def broken(file: pypi.PypiFile, into: pathlib.Path) -> pathlib.Path:
        raise pypi.IntegrityError('sha256 mismatch')

    monkeypatch.setattr(pypi, 'download', broken)
    out: sign.SignOutcome = sign.sign_release(
        release([]), files, tmp_path, options
    )
    assert out.status == 'failed'
    assert out.reason == 'sha256 mismatch'
    assert world.uploads == []


def project_with(releases: dict[str, list[pypi.PypiFile]]) -> pypi.Project:
    return pypi.Project(name='s', latest='1.0', releases=releases, repo='o/r')


def test_releases_missing_signatures_picks_releases_without_asc(
    monkeypatch: pytest.MonkeyPatch,
    files: list[pypi.PypiFile],
) -> None:
    signed: github.Release = release(
        ['s.whl', 's.whl.asc', 's.tar.gz', 's.tar.gz.asc'], tag='v1.0'
    )
    half: github.Release = release(['s.whl', 's.whl.asc'], tag='v2.0')
    bare: github.Release = release([], tag='v3.0')
    stray: github.Release = release([], tag='v9.9')
    monkeypatch.setattr(
        github, 'releases', lambda repo: [signed, half, bare, stray]
    )
    project: pypi.Project = project_with(
        {'1.0': files, '2.0': files, '3.0': files, '4.0': []}
    )
    missing: list[github.Release] = sign.releases_missing_signatures(
        'o/r', project
    )
    assert [r.tag for r in missing] == ['v2.0', 'v3.0']


def test_main_signs_one_tag_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    files: list[pypi.PypiFile],
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, _ = install(monkeypatch)
    monkeypatch.setattr(
        pypi, 'project', lambda package: project_with({'1.0': files})
    )
    monkeypatch.setattr(github, 'releases', lambda repo: [release([])])
    code: int = sign.main(['v1.0', '--package', 's'])
    assert code == 0
    assert 'v1.0: signed (uploaded 8, kept 0, conflicts 0)' in (
        capsys.readouterr().out
    )
    assert sorted(world.uploaded) == sorted(ALL_NAMES)


def test_main_exits_one_when_a_version_fails(
    monkeypatch: pytest.MonkeyPatch,
    files: list[pypi.PypiFile],
    capsys: pytest.CaptureFixture[str],
) -> None:
    install(monkeypatch)

    def broken(file: pypi.PypiFile, into: pathlib.Path) -> pathlib.Path:
        raise pypi.IntegrityError('bad hash')

    monkeypatch.setattr(pypi, 'download', broken)
    monkeypatch.setattr(
        pypi, 'project', lambda package: project_with({'1.0': files})
    )
    monkeypatch.setattr(github, 'releases', lambda repo: [release([])])
    assert sign.main(['--all', '--package', 's']) == 1
    printed: str = capsys.readouterr().out
    assert 'v1.0: failed' in printed
    assert 'bad hash' in printed


def test_main_refuses_without_package_or_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert sign.main(['v1.0']) == 2
    monkeypatch.setattr(
        pypi,
        'project',
        lambda package: pypi.Project('s', '1.0', {'1.0': files}, None),
    )
    assert sign.main(['v1.0', '--package', 's']) == 2
    assert 'no --repo' in capsys.readouterr().err


def test_gpg_only_and_sigstore_only_are_exclusive() -> None:
    with pytest.raises(SystemExit) as raised:
        sign.main(['v1.0', '--gpg-only', '--sigstore-only'])
    assert raised.value.code == 2


def test_sign_is_registered() -> None:
    assert cli.COMMANDS['sign'] is sign.main


def test_only_missing_signatures_are_produced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    world, _ = install(
        monkeypatch, {'s.whl': WHL_BYTES, 's.tar.gz': SDIST_BYTES}
    )
    have: list[str] = [
        n for n in ALL_NAMES if n not in ('s.whl.asc', 'SHA256SUMS.asc')
    ]
    out: sign.SignOutcome = sign.sign_release(
        release(have, body=sign.FOOTER_MARKER), files, tmp_path, options
    )
    assert sorted(out.uploaded) == ['SHA256SUMS.asc', 's.whl.asc']
    assert world.sigstore_calls == 0
    assert world.signed == ['s.whl', 'SHA256SUMS']


def test_sha256sums_lists_dists_and_rebuilt_comment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    import hashlib

    install(monkeypatch)
    captured: list[str] = []
    real: typing.Callable[..., pathlib.Path] = signing.write_sha256sums

    def spy(
        directory: pathlib.Path,
        paths: list[pathlib.Path],
        rebuilt: dict[str, str],
    ) -> pathlib.Path:
        target: pathlib.Path = real(directory, paths, rebuilt)
        captured.append(target.read_text())
        return target

    monkeypatch.setattr(signing, 'write_sha256sums', spy)
    options.rebuilt = {'s.whl': '# s.whl rebuilt'}
    sign.sign_release(release([]), files, tmp_path, options)
    whl: str = hashlib.sha256(WHL_BYTES).hexdigest()
    sdist: str = hashlib.sha256(SDIST_BYTES).hexdigest()
    assert captured == [f'# s.whl rebuilt\n{whl}  s.whl\n{sdist}  s.tar.gz\n']


def test_release_workdir_is_removed_after_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    files: list[pypi.PypiFile],
    options: sign.SignOptions,
) -> None:
    install(monkeypatch)
    project: pypi.Project = project_with({'1.0': files})
    sign._sign_one(release([]), project, tmp_path, options)
    assert not (tmp_path / 'v1.0').exists()

    def broken(file: pypi.PypiFile, into: pathlib.Path) -> pathlib.Path:
        into.mkdir(parents=True, exist_ok=True)
        (into / file.filename).write_bytes(b'partial')
        raise pypi.IntegrityError('bad hash')

    monkeypatch.setattr(pypi, 'download', broken)
    out: sign.SignOutcome = sign._sign_one(
        release([]), project, tmp_path, options
    )
    assert out.status == 'failed'
    assert not (tmp_path / 'v1.0').exists()


def test_all_continues_after_a_signing_error(
    monkeypatch: pytest.MonkeyPatch,
    files: list[pypi.PypiFile],
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, _ = install(monkeypatch)
    real: typing.Callable[..., pathlib.Path] = signing.gpg_sign

    def flaky(path: pathlib.Path, key: str) -> pathlib.Path:
        if 'v1.0' in str(path):
            raise signing.SigningError('gpg exploded')
        return real(path, key)

    monkeypatch.setattr(signing, 'gpg_sign', flaky)
    monkeypatch.setattr(
        pypi,
        'project',
        lambda package: project_with({'1.0': files, '2.0': files}),
    )
    monkeypatch.setattr(
        github,
        'releases',
        lambda repo: [release([], tag='v1.0'), release([], tag='v2.0')],
    )
    assert sign.main(['--all', '--package', 's']) == 1
    printed: str = capsys.readouterr().out
    assert 'v1.0: failed' in printed
    assert 'gpg exploded' in printed
    assert 'v2.0: signed' in printed
    assert world.uploaded
