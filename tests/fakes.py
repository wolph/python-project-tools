"""Test doubles shared by the command tests."""

from __future__ import annotations

import pathlib
import subprocess


def completed(
    stdout: str = '', returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """Build a canned reply for the Recorder."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=''
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
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        joined: str = self._joined(args)
        matches: list[str] = [
            key for key in self.replies if joined.startswith(key)
        ]
        if not matches:
            raise AssertionError(f'no canned reply for argv {args!r}')
        return self.replies[max(matches, key=len)]
