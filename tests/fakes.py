"""Test doubles shared by the command tests."""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import subprocess


def covering_bundle(path: pathlib.Path) -> str:
    """The smallest bundle JSON whose message digest is that of ``path``.

    Enough for ``sigstore_signer.bundle_covers``, which compares only the
    digest. Real bundles carry the certificate and the log entry as well.
    """
    digest: bytes = hashlib.sha256(path.read_bytes()).digest()
    return json.dumps(
        {
            'mediaType': 'application/vnd.dev.sigstore.bundle.v0.3+json',
            'messageSignature': {
                'messageDigest': {
                    'algorithm': 'SHA2_256',
                    'digest': base64.b64encode(digest).decode(),
                },
                'signature': 'c3BhbQ==',
            },
        }
    )


def completed(
    stdout: str = '', returncode: int = 0, stderr: str = ''
) -> subprocess.CompletedProcess[str]:
    """Build a canned reply for the Recorder."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class Recorder:
    """Stand-in for common.run that replays canned results and records calls.

    Keys of ``replies`` are joined argv prefixes. The argv is cut at the
    first token starting with ``--notes-file`` before joining, so temporary
    paths never take part in matching. The longest matching key wins.
    """

    def __init__(
        self, replies: dict[str, subprocess.CompletedProcess[str]]
    ) -> None:
        self.replies: dict[str, subprocess.CompletedProcess[str]] = replies
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.timeouts: list[float | None] = []

    @staticmethod
    def _joined(args: list[str]) -> str:
        kept: list[str] = []
        for token in args:
            if token.startswith('--notes-file'):
                break
            kept.append(token)
        return ' '.join(kept)

    def __call__(
        self,
        args: list[str],
        check: bool = False,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        self.envs.append(env)
        self.timeouts.append(timeout)
        joined: str = self._joined(args)
        matches: list[str] = [
            key for key in self.replies if joined.startswith(key)
        ]
        if not matches:
            raise AssertionError(f'no canned reply for argv {args!r}')
        return self.replies[max(matches, key=len)]
