"""Signing mechanics: gpg, sigstore and SHA256SUMS.

Pure mechanics with no policy. Every subprocess goes through
``common.run`` so tests can replace one attribute.
"""

from __future__ import annotations

import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile

from python_project_tools import common

DEFAULT_GPG_KEY: str = '149325FD15904E9C4EB89E95E81444E9CE1F695D'
# Pinned, so a new sigstore release cannot change what a run signs with.
SIGSTORE_ARGV: list[str] = ['uvx', 'sigstore==4.5.1']
SHA256SUMS_NAME: str = 'SHA256SUMS'
BUNDLE_SUFFIX: str = '.sigstore.json'
# The OIDC login waits for a browser callback that may never come.
SIGSTORE_TIMEOUT_SECONDS: int = 900
SIGSTORE_TIMEOUT_MESSAGE: str = (
    'sigstore did not finish within {seconds} s, the browser login '
    'probably never reached it'
)
SIGSTORE_LOGIN_NOTICE: str = (
    'ppt: Sigstore login needed, a browser tab opens for {count} files'
)
GPG_STATUS_PREFIX: str = '[GNUPG:] '
# Status keywords that rule a signature out even next to a good one.
GPG_REJECTED_STATUSES: frozenset[str] = frozenset(
    {'BADSIG', 'ERRSIG', 'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG', 'NO_PUBKEY'}
)


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


def gpg_fingerprint(key: str) -> str | None:
    """The full fingerprint of the one primary key ``key`` names.

    None when gpg knows no such key or the name matches several keys, so
    a vague name never picks one of them.
    """
    done: subprocess.CompletedProcess[str] = common.run(
        ['gpg', '--batch', '--with-colons', '--list-keys', '--', key]
    )
    if done.returncode != 0:
        return None
    found: list[str] = []
    after_pub: bool = False
    for line in done.stdout.splitlines():
        fields: list[str] = line.split(':')
        if fields[0] == 'fpr' and after_pub and len(fields) > 9:
            found.append(fields[9].upper())
        after_pub = fields[0] == 'pub'
    return found[0] if len(found) == 1 else None


def _status_lines(stdout: str) -> list[list[str]]:
    """The ``[GNUPG:]`` lines of ``--status-fd`` output, split on spaces."""
    return [
        line.split()[1:]
        for line in stdout.splitlines()
        if line.startswith(GPG_STATUS_PREFIX) and len(line.split()) > 1
    ]


def _signed_by(statuses: list[list[str]], fingerprint: str) -> bool:
    """A good signature whose key or primary key is ``fingerprint``.

    VALIDSIG names the signing key first and its primary key last, so a
    signature made by a signing subkey of the key counts as well.
    """
    if any(status[0] in GPG_REJECTED_STATUSES for status in statuses):
        return False
    if not any(status[0] == 'GOODSIG' for status in statuses):
        return False
    for status in statuses:
        if status[0] != 'VALIDSIG' or len(status) < 2:
            continue
        signers: set[str] = {status[1].upper(), status[-1].upper()}
        if fingerprint in signers:
            return True
    return False


def gpg_verify_key(
    path: pathlib.Path, signature: pathlib.Path, key: str
) -> bool:
    """True when ``signature`` is a good signature of ``path`` by ``key``.

    ``key`` is resolved to its full fingerprint first, and the VALIDSIG
    status line must name exactly that fingerprint. Any other good
    signature, an expired or revoked key and an unknown key all fail.
    """
    fingerprint: str | None = gpg_fingerprint(key)
    if fingerprint is None:
        return False
    done: subprocess.CompletedProcess[str] = common.run(
        [
            'gpg',
            '--batch',
            '--status-fd',
            '1',
            '--verify',
            str(signature),
            str(path),
        ]
    )
    if done.returncode != 0:
        return False
    return _signed_by(_status_lines(done.stdout), fingerprint)


def bundle_path(path: pathlib.Path) -> pathlib.Path:
    """Where the Sigstore bundle of a file lives: ``<file>.sigstore.json``."""
    return path.with_name(path.name + BUNDLE_SUFFIX)


def relay_browser() -> str:
    """The BROWSER value that routes sigstore's login through the relay.

    Python's ``webbrowser`` runs an entry containing ``%s`` as a command
    with the URL in its place, so the relay prints the URL and then opens
    the real browser. See ``browser_relay``.
    """
    return (
        f'{shlex.quote(sys.executable)} -m python_project_tools.browser_relay'
        ' %s'
    )


def _sigstore_run(directory: pathlib.Path, paths: list[pathlib.Path]) -> None:
    """One sigstore invocation for files that all live in ``directory``.

    Output is not captured, so the browser prompt and the fallback URL of
    the OIDC flow reach the terminal. A notice on stderr comes first, so
    whoever watches a log knows a person is needed. An invocation still
    running after SIGSTORE_TIMEOUT_SECONDS is stopped and raises
    RuntimeError: a login callback that never arrives would otherwise
    keep it listening forever.
    """
    print(  # noqa: T201
        SIGSTORE_LOGIN_NOTICE.format(count=len(paths)),
        file=sys.stderr,
        flush=True,
    )
    done: subprocess.CompletedProcess[str]
    try:
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
            timeout=SIGSTORE_TIMEOUT_SECONDS,
            env={'BROWSER': relay_browser()},
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            SIGSTORE_TIMEOUT_MESSAGE.format(seconds=SIGSTORE_TIMEOUT_SECONDS)
        ) from error
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


def sha256sums_comments(path: pathlib.Path) -> dict[str, str]:
    """``{name: comment}`` for each entry with a comment line right above.

    The inverse of the ``rebuilt`` argument of ``write_sha256sums``.
    """
    comments: dict[str, str] = {}
    comment: str = ''
    for line in path.read_text().splitlines():
        if line.startswith('#'):
            comment = line
            continue
        if not line.strip():
            comment = ''
            continue
        _, _, name = line.partition('  ')
        if comment:
            comments[name] = comment
        comment = ''
    return comments
