"""Shared fixtures: a throwaway GNUPGHOME and a scratch git repository."""

from __future__ import annotations

import collections.abc
import dataclasses
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest


@pytest.fixture
def gnupg_home(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> collections.abc.Iterator[str]:
    """A throwaway GNUPGHOME with one ed25519 key, yielding its fingerprint."""
    if sys.platform == 'win32':
        pytest.skip(
            'gpg-agent sockets are not reliable on the Windows runners'
        )
    if shutil.which('gpg') is None:
        pytest.skip('gpg unusable here: gpg is not installed')
    # gpg-agent sockets live under GNUPGHOME and unix socket paths cap at
    # about 104 bytes, which pytest's tmp_path on macOS already exceeds.
    # mkdtemp gives a short directory with mode 0o700.
    home: pathlib.Path = pathlib.Path(tempfile.mkdtemp(prefix='ppt-gpg-'))
    try:
        monkeypatch.setenv('GNUPGHOME', str(home))
        generated: subprocess.CompletedProcess[str] = subprocess.run(
            [
                'gpg',
                '--batch',
                '--passphrase',
                '',
                '--quick-generate-key',
                'ppt test <ppt@example.invalid>',
                'ed25519',
                'sign',
                '1d',
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if generated.returncode != 0:
            pytest.skip(f'gpg unusable here: {generated.stderr.strip()}')
        listed: subprocess.CompletedProcess[str] = subprocess.run(
            ['gpg', '--batch', '--list-secret-keys', '--with-colons'],
            check=False,
            capture_output=True,
            text=True,
        )
        if listed.returncode != 0:
            pytest.skip(f'gpg unusable here: {listed.stderr.strip()}')
        fingerprint: str = next(
            line.split(':')[9]
            for line in listed.stdout.splitlines()
            if line.startswith('fpr:')
        )
        yield fingerprint
    finally:
        subprocess.run(['gpgconf', '--kill', 'gpg-agent'], check=False)
        shutil.rmtree(home, ignore_errors=True)


@dataclasses.dataclass
class ScratchRepo:
    """A small git repository with one version bump per commit."""

    path: pathlib.Path
    shas: list[str]  # oldest first: versions 1.0, 1.1, 1.2
    origin: pathlib.Path  # a bare repository with master pushed


def git(path: pathlib.Path, *args: str) -> str:
    """Run git in a scratch repository and return stdout."""
    return subprocess.run(
        ['git', *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def scratch_repo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> ScratchRepo:
    """Three commits bumping __version__ and setup.py to 1.0, 1.1, 1.2.

    The repository has a bare ``origin`` next to it that holds master.
    """
    # Keep the user's global git config (signing, hooks) out of the repo.
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'no-such-config'))
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'no-such-config'))
    path: pathlib.Path = tmp_path / 'repo'
    (path / 'spam').mkdir(parents=True)
    git(path, 'init', '-q', '-b', 'master')
    git(path, 'config', 'user.name', 'Spam')
    git(path, 'config', 'user.email', 'spam@example.invalid')
    git(path, 'config', 'commit.gpgsign', 'false')
    (path / 'README.md').write_text('spam and eggs\n')
    shas: list[str] = []
    for version in ('1.0', '1.1', '1.2'):
        (path / 'spam' / '__init__.py').write_text(
            f"__version__ = '{version}'\n"
        )
        (path / 'setup.py').write_text(
            'from setuptools import setup\n'
            f"setup(name='spam', version='{version}')\n"
        )
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', f'bump {version}')
        shas.append(git(path, 'rev-parse', 'HEAD'))
    origin: pathlib.Path = tmp_path / 'origin.git'
    git(tmp_path, 'init', '-q', '--bare', '-b', 'master', str(origin))
    git(path, 'remote', 'add', 'origin', str(origin))
    git(path, 'push', '-q', 'origin', 'master')
    return ScratchRepo(path=path, shas=shas, origin=origin)
