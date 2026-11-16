"""Check who signed a Sigstore bundle, inside the pinned sigstore environment.

This script runs the way ``sigstore_signer`` does::

    uvx --from sigstore==4.5.1 python -I bundle_verifier.py
        --manifest FILE --repo OWNER/NAME --output FILE

so it imports the standard library only at import time, and sigstore only
inside the functions that talk to it. ``identities`` imports this module for
its path, which is why nothing here runs on import.

The manifest is a JSON list of ``{"name": NAME, "sha256": HEX, "bundle":
PATH}``. The output is a JSON object that maps every name to ``"ci"`` (the
sign.yml workflow of the repository, started by hand on a branch),
``"rick"`` (a login of the maintainer) or ``null`` (neither, or a bundle that
does not cover that digest).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import typing

CI_ISSUER: str = 'https://token.actions.githubusercontent.com'
RICK_ISSUER: str = 'https://github.com/login/oauth'
RICK_IDENTITY: str = 'wolph@wol.ph'

EXIT_USAGE: int = 2

SOURCE_REPOSITORY_OID: str = '1.3.6.1.4.1.57264.1.12'
SHA256_PATTERN: re.Pattern[str] = re.compile(r'[0-9a-fA-F]{64}')

_CI_SAN: re.Pattern[str] = re.compile(
    r'https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)'
    r'/\.github/workflows/sign\.yml@refs/heads/(?P<branch>.+)'
)


def ci_identity_matches(san: str, repo: str) -> bool:
    """Whether ``san`` is sign.yml of ``repo`` on a branch.

    GitHub names are case-insensitive, so the repository is compared that
    way. A tag ref or an empty branch does not match.
    """
    match: re.Match[str] | None = _CI_SAN.fullmatch(san)
    if match is None:
        return False
    return match.group('repo').lower() == repo.lower()


def decode_utf8string(raw: bytes) -> str:
    """Decode a DER UTF8String, as the 1.8 and later extensions hold."""
    if len(raw) < 2 or raw[0] != 0x0C:
        raise ValueError('not a DER UTF8String')
    length: int = raw[1]
    start: int = 2
    if length & 0x80:
        count: int = length & 0x7F
        length = int.from_bytes(raw[2 : 2 + count], 'big')
        start = 2 + count
    body: bytes = raw[start:]
    if len(body) != length:
        raise ValueError('DER length does not match')
    return body.decode()


def ci_policy(repo: str) -> typing.Any:
    """The policy for a sign.yml run of ``repo`` started by hand."""
    from cryptography import x509  # ty: ignore[import-not-found]
    from sigstore.errors import (  # ty: ignore[import-not-found]
        VerificationError,
    )
    from sigstore.verify.policy import (  # ty: ignore[import-not-found]
        AllOf,
        GitHubWorkflowTrigger,
        OIDCIssuerV2,
    )

    class SignYmlSan:
        """Policy: the URI SAN of the certificate is sign.yml of ``repo``."""

        def verify(self, cert: x509.Certificate) -> None:
            ext: x509.SubjectAlternativeName = (
                cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value
            )
            sans: list[str] = ext.get_values_for_type(
                x509.UniformResourceIdentifier
            )
            if not any(ci_identity_matches(san, repo) for san in sans):
                raise VerificationError(
                    f'no sign.yml identity of {repo} in {sans}'
                )

    class SourceRepository:
        """Policy: the source repository extension names ``repo``."""

        def verify(self, cert: x509.Certificate) -> None:
            try:
                raw: bytes = cert.extensions.get_extension_for_oid(
                    x509.ObjectIdentifier(SOURCE_REPOSITORY_OID)
                ).value.value
                found: str = decode_utf8string(raw)
            except (x509.ExtensionNotFound, ValueError) as error:
                raise VerificationError(
                    f'no usable source repository: {error}'
                ) from error
            if found.lower() != f'https://github.com/{repo}'.lower():
                raise VerificationError(
                    f'source repository {found} is not {repo}'
                )

    return AllOf(
        [
            OIDCIssuerV2(CI_ISSUER),
            GitHubWorkflowTrigger('workflow_dispatch'),
            SourceRepository(),
            SignYmlSan(),
        ]
    )


def _rick_policy() -> typing.Any:
    from sigstore.verify.policy import Identity  # ty: ignore[import-not-found]

    return Identity(identity=RICK_IDENTITY, issuer=RICK_ISSUER)


def verify_entries(
    entries: list[dict[str, str]], repo: str
) -> dict[str, str | None]:
    """The verdict per entry name, the CI policy tried before Rick's."""
    from sigstore.errors import (  # ty: ignore[import-not-found]
        VerificationError,
    )
    from sigstore.hashes import (  # ty: ignore[import-not-found]
        HashAlgorithm,
        Hashed,
    )
    from sigstore.models import (  # ty: ignore[import-not-found]
        Bundle,
        InvalidBundle,
    )
    from sigstore.verify import Verifier  # ty: ignore[import-not-found]

    verifier: typing.Any = Verifier.production()
    policies: list[tuple[str, typing.Any]] = [
        ('ci', ci_policy(repo)),
        ('rick', _rick_policy()),
    ]
    verdicts: dict[str, str | None] = {}
    for entry in entries:
        verdicts[entry['name']] = None
        raw: bytes = pathlib.Path(entry['bundle']).read_bytes()
        try:
            bundle: typing.Any = Bundle.from_json(raw)
        except (InvalidBundle, ValueError):
            continue
        digest: typing.Any = Hashed(
            algorithm=HashAlgorithm.SHA2_256,
            digest=bytes.fromhex(entry['sha256']),
        )
        for identity, policy in policies:
            try:
                verifier.verify_artifact(digest, bundle, policy)
            except (VerificationError, InvalidBundle):
                continue
            verdicts[entry['name']] = identity
            break
    return verdicts


def main(argv: list[str]) -> int:
    """Run the helper, return the exit code."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog='bundle_verifier', exit_on_error=False
    )
    parser.add_argument('--manifest', type=pathlib.Path, required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    try:
        args: argparse.Namespace = parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return EXIT_USAGE
    entries: list[dict[str, str]] = json.loads(args.manifest.read_text())
    if not all(SHA256_PATTERN.fullmatch(entry['sha256']) for entry in entries):
        print('sha256 must be 64 hex characters', file=sys.stderr)  # noqa: T201
        return EXIT_USAGE
    verdicts: dict[str, str | None] = verify_entries(entries, args.repo)
    args.output.write_text(json.dumps(verdicts))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
