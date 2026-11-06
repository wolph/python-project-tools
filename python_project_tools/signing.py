"""Signing mechanics: gpg, sigstore and SHA256SUMS.

Pure mechanics with no policy. Every subprocess goes through
``common.run`` so tests can replace one attribute.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile

from python_project_tools import common

DEFAULT_GPG_KEY: str = '149325FD15904E9C4EB89E95E81444E9CE1F695D'
# Pinned, so a new sigstore release cannot change what a run signs with.
SIGSTORE_ARGV: list[str] = ['uvx', 'sigstore==4.5.1']
SHA256SUMS_NAME: str = 'SHA256SUMS'
BUNDLE_SUFFIX: str = '.sigstore.json'


class SigningError(Exception):
    """A signing tool failed or did not produce its output."""


def gpg_sign(path: pathlib.Path, key: str = DEFAULT_GPG_KEY) -> pathlib.Path:
    """Write an ASCII armoured detached signature to ``<path>.asc``."""
    signature: pathlib.Path = path.with_name(path.name + '.asc')
    done = common.run(
        [
            'gpg',
            '--batch',
            '--yes',
            '--armor',
            '--detach-sign',
            '--local-user',
            key,
            '--output',
            str(signature),
            str(path),
        ]
    )
    if done.returncode != 0:
        raise SigningError(
            f'gpg failed for {path.name}: {done.stderr.strip()}'
        )
    if not signature.exists():
        raise SigningError(f'gpg wrote no signature for {path.name}')
    return signature


def gpg_verify(path: pathlib.Path, signature: pathlib.Path) -> bool:
    """True when ``signature`` is a good signature of ``path``."""
    done = common.run(
        ['gpg', '--batch', '--verify', str(signature), str(path)]
    )
    return done.returncode == 0


def bundle_path(path: pathlib.Path) -> pathlib.Path:
    """Where the Sigstore bundle of a file lives: ``<file>.sigstore.json``."""
    return path.with_name(path.name + BUNDLE_SUFFIX)


def _sigstore_run(directory: pathlib.Path, paths: list[pathlib.Path]) -> None:
    """One sigstore invocation for files that all live in ``directory``.

    Output is not captured, so the browser prompt and the fallback URL of
    the OIDC flow reach the terminal.
    """
    done = common.run(
        [
            *SIGSTORE_ARGV,
            'sign',
            '--output-directory',
            str(directory),
            '--overwrite',
            *map(str, paths),
        ],
        capture=False,
    )
    if done.returncode != 0:
        detail: str = (done.stderr or '').strip() or 'see its output above'
        raise SigningError(
            f'sigstore failed with exit status {done.returncode}: {detail}'
        )


def _existing_bundles(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    bundles: list[pathlib.Path] = [bundle_path(path) for path in paths]
    missing: list[str] = [b.name for b in bundles if not b.exists()]
    if missing:
        raise SigningError(
            'sigstore wrote no bundle for ' + ', '.join(missing)
        )
    return bundles


def _sign_staged(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    """Copy files from several directories into one, sign, move back.

    The bundle covers the file's bytes, not its path, so a bundle made for
    the staged copy is the bundle of the original.
    """
    names: list[str] = [path.name for path in paths]
    duplicates: list[str] = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise SigningError(
            'sigstore staging needs unique file names, got '
            + ', '.join(duplicates)
        )
    with tempfile.TemporaryDirectory(prefix='ppt-sigstore-') as directory:
        staging: pathlib.Path = pathlib.Path(directory)
        staged: list[pathlib.Path] = []
        for path in paths:
            copy: pathlib.Path = staging / path.name
            shutil.copy2(path, copy)
            staged.append(copy)
        _sigstore_run(staging, staged)
        bundles: list[pathlib.Path] = _existing_bundles(staged)
        targets: list[pathlib.Path] = [bundle_path(path) for path in paths]
        for bundle, target in zip(bundles, targets, strict=True):
            shutil.move(bundle, target)
    return targets


def sigstore_sign(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    """Sign all files in one sigstore invocation, return the bundle paths.

    The OIDC flow is interactive, so one invocation means one login. Files
    from several directories are staged in one directory first, so they
    still take one invocation, and their names must then be unique. Each
    bundle ends up next to its file.
    """
    if not paths:
        return []
    parents: set[pathlib.Path] = {path.parent for path in paths}
    if len(parents) != 1:
        return _sign_staged(paths)
    _sigstore_run(paths[0].parent, paths)
    return _existing_bundles(paths)


def write_sha256sums(
    directory: pathlib.Path,
    files: list[pathlib.Path],
    rebuilt: dict[str, str],
) -> pathlib.Path:
    """Write ``directory/SHA256SUMS`` in ``sha256sum -c`` format.

    ``rebuilt`` maps a file name to a comment line placed above its entry.
    """
    lines: list[str] = []
    for path in files:
        if path.name in rebuilt:
            lines.append(rebuilt[path.name])
        lines.append(f'{common.sha256_of(path)}  {path.name}')
    target: pathlib.Path = directory / SHA256SUMS_NAME
    target.write_text('\n'.join(lines) + '\n')
    return target


def parse_sha256sums(path: pathlib.Path) -> dict[str, str]:
    """Read a SHA256SUMS file into ``{name: sha256}``, skipping comments."""
    sums: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        digest, _, name = line.partition('  ')
        sums[name] = digest
    return sums
