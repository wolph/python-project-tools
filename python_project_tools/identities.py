"""Who signed a Sigstore bundle: the CI workflow of the repository or Rick.

The check runs in the pinned sigstore environment, see ``bundle_verifier``.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import subprocess
import tempfile
import typing

from python_project_tools import bundle_verifier, common, signing

Identity = typing.Literal['ci', 'rick']

VERIFIER_SCRIPT: pathlib.Path = pathlib.Path(bundle_verifier.__file__)


@dataclasses.dataclass(frozen=True)
class BundleCheck:
    """One bundle to check: the file name, its sha256 and the bundle path."""

    name: str
    sha256: str
    bundle: pathlib.Path

    def __post_init__(self) -> None:
        """Reject a sha256 that is not 64 hex characters."""
        if not bundle_verifier.SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError(f'sha256 of {self.name} is not 64 hex characters')


def verify_bundles(
    checks: list[BundleCheck], repo: str
) -> dict[str, Identity | None]:
    """The signer of every check in one helper run, by name.

    ``repo`` is ``OWNER/NAME``, the repository whose sign.yml counts as CI.
    A name maps to None when its bundle matches neither identity. Raises
    SigningError when the helper fails or returns anything but one verdict
    per name, ValueError for duplicate names.
    """
    names: list[str] = [check.name for check in checks]
    if len(set(names)) != len(names):
        raise ValueError('duplicate names in bundle checks')
    with tempfile.TemporaryDirectory(prefix='ppt-verify-') as directory:
        manifest: pathlib.Path = pathlib.Path(directory) / 'manifest.json'
        output: pathlib.Path = pathlib.Path(directory) / 'output.json'
        manifest.write_text(
            json.dumps(
                [
                    {
                        'name': check.name,
                        'sha256': check.sha256,
                        'bundle': str(check.bundle),
                    }
                    for check in checks
                ]
            )
        )
        argv: list[str] = [
            *signing.SIGSTORE_ARGV,
            str(VERIFIER_SCRIPT),
            '--manifest',
            str(manifest),
            '--repo',
            repo,
            '--output',
            str(output),
        ]
        done: subprocess.CompletedProcess[str] = common.run(argv)
        if done.returncode != 0:
            raise signing.SigningError(
                f'bundle verification failed: {done.stderr.strip()}'
            )
        verdicts: object = json.loads(output.read_text())
    if (
        not isinstance(verdicts, dict)
        or set(verdicts) != set(names)
        or not all(
            value in ('ci', 'rick', None) for value in verdicts.values()
        )
    ):
        raise signing.SigningError(
            f'bundle verification returned unexpected output: {verdicts!r}'
        )
    return verdicts
