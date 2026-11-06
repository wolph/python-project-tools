"""Tests for the tag rebuild helper."""

from __future__ import annotations

import io
import pathlib
import subprocess
import tarfile

import pytest

from python_project_tools import common, rebuild
from tests.fakes import completed

TREE: str = 'spam.txt'


class FakeRun:
    """Handle git archive and uv build the way the real tools would."""

    def __init__(self, uv_returncode: int = 0, build: bool = True) -> None:
        self.calls: list[tuple[list[str], pathlib.Path | None]] = []
        self.uv_returncode: int = uv_returncode
        self.build: bool = build
        self.tree_files: list[str] = []

    def __call__(
        self,
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), cwd))
        if args[:2] == ['git', 'archive']:
            return self._archive(args)
        return self._uv_build(args, cwd)

    @staticmethod
    def _archive(args: list[str]) -> subprocess.CompletedProcess[str]:
        target: str = args[args.index('--output') + 1]
        with tarfile.open(target, 'w') as tar:
            data: bytes = b'eggs\n'
            info: tarfile.TarInfo = tarfile.TarInfo(TREE)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return completed()

    def _uv_build(
        self, args: list[str], cwd: pathlib.Path | None
    ) -> subprocess.CompletedProcess[str]:
        assert cwd is not None
        self.tree_files = sorted(p.name for p in cwd.iterdir())
        if self.uv_returncode:
            return subprocess.CompletedProcess(
                args, self.uv_returncode, '', 'build exploded'
            )
        out: pathlib.Path = pathlib.Path(args[args.index('--out-dir') + 1])
        if self.build:
            (out / 'spam-1.0-py3-none-any.whl').write_bytes(b'whl')
            (out / 'spam-1.0.tar.gz').write_bytes(b'sdist')
        return completed()


def test_rebuild_runs_uv_build_in_an_archive(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeRun = FakeRun()
    monkeypatch.setattr(common, 'run', fake)
    checkout: pathlib.Path = tmp_path / 'checkout'
    checkout.mkdir()
    out: pathlib.Path = tmp_path / 'work' / 'v1.0-rebuilt'
    built: list[pathlib.Path] = rebuild.rebuild(checkout, 'v1.0', out)
    assert sorted(p.name for p in built) == [
        'spam-1.0-py3-none-any.whl',
        'spam-1.0.tar.gz',
    ]
    assert all(p.parent == out and p.exists() for p in built)
    archive, uv = fake.calls
    assert archive[0][:4] == ['git', 'archive', '--format=tar', '--output']
    assert archive[0][-1] == 'v1.0'
    assert archive[1] == checkout
    assert uv[0][:2] == ['uv', 'build']
    assert uv[0][uv[0].index('--out-dir') + 1] == str(out)
    assert uv[1] is not None
    assert uv[1] != checkout
    assert fake.tree_files == [TREE]


def test_rebuild_removes_the_extraction_directory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeRun = FakeRun()
    monkeypatch.setattr(common, 'run', fake)
    rebuild.rebuild(tmp_path, 'v1.0', tmp_path / 'out')
    uv_cwd: pathlib.Path | None = fake.calls[1][1]
    assert uv_cwd is not None
    assert not uv_cwd.exists()


def test_rebuild_raises_with_stderr_on_a_failed_build(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, 'run', FakeRun(uv_returncode=1))
    with pytest.raises(rebuild.RebuildError, match='build exploded'):
        rebuild.rebuild(tmp_path, 'v1.0', tmp_path / 'out')


def test_rebuild_raises_when_nothing_was_built(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, 'run', FakeRun(build=False))
    with pytest.raises(rebuild.RebuildError, match='no wheel or sdist'):
        rebuild.rebuild(tmp_path, 'v1.0', tmp_path / 'out')


def test_rebuild_raises_when_the_tag_cannot_be_archived(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 128, '', 'fatal: not a valid object name'
        )

    monkeypatch.setattr(common, 'run', failing)
    with pytest.raises(rebuild.RebuildError, match='not a valid object'):
        rebuild.rebuild(tmp_path, 'v9.9', tmp_path / 'out')
