"""Signing mechanics: gpg, sigstore and SHA256SUMS.

Pure mechanics with no policy. Every subprocess goes through
``common.run`` so tests can replace one attribute.
"""

from __future__ import annotations

import pathlib

from python_project_tools import common

DEFAULT_GPG_KEY: str = '149325FD15904E9C4EB89E95E81444E9CE1F695D'
SIGSTORE_ARGV: list[str] = ['uvx', 'sigstore']
SHA256SUMS_NAME: str = 'SHA256SUMS'


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


def sigstore_sign(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    """Sign all files in one sigstore invocation, return the bundle paths.

    The OIDC flow is interactive, so one invocation means one login.
    """
    if not paths:
        return []
    parents: set[pathlib.Path] = {path.parent for path in paths}
    if len(parents) != 1:
        raise SigningError(
            'sigstore needs all files in one directory, got '
            + ', '.join(sorted(str(parent) for parent in parents))
        )
    done = common.run(
        [
            *SIGSTORE_ARGV,
            'sign',
            '--output-directory',
            str(paths[0].parent),
            '--overwrite',
            *map(str, paths),
        ]
    )
    if done.returncode != 0:
        raise SigningError(f'sigstore failed: {done.stderr.strip()}')
    bundles: list[pathlib.Path] = [
        path.with_name(path.name + '.sigstore.json') for path in paths
    ]
    missing: list[str] = [b.name for b in bundles if not b.exists()]
    if missing:
        raise SigningError(
            'sigstore wrote no bundle for ' + ', '.join(missing)
        )
    return bundles


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
