"""Run the CI policy of bundle_verifier on certificates, inside sigstore.

Usage, under ``uvx --from sigstore==4.5.1 python -I``::

    policy_cases.py BUNDLE_VERIFIER_PATH PROVENANCE_JSON

Prints a JSON object that maps every case to whether the policy for
``WoLpH/python-utils`` accepted its certificate.
"""

from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import json
import pathlib
import sys
import types
import typing

from cryptography import x509  # ty: ignore[import-not-found]
from cryptography.hazmat.primitives import (  # ty: ignore[import-not-found]
    hashes,
)
from cryptography.hazmat.primitives.asymmetric import (  # ty: ignore[import-not-found]
    ec,
)
from cryptography.x509.oid import NameOID  # ty: ignore[import-not-found]
from sigstore.errors import VerificationError  # ty: ignore[import-not-found]

REPO: str = 'WoLpH/python-utils'
SIGN_YML: str = (
    'https://github.com/WoLpH/python-utils/.github/workflows/sign.yml'
    '@refs/heads/develop'
)
ISSUER: str = 'https://token.actions.githubusercontent.com'


def der_string(value: str) -> bytes:
    raw: bytes = value.encode()
    assert len(raw) < 128
    return bytes([0x0C, len(raw)]) + raw


def make_cert(
    sans: list[x509.GeneralName],
    trigger: bytes = b'workflow_dispatch',
    source: str | None = 'https://github.com/WoLpH/python-utils',
    issuer: str = ISSUER,
) -> x509.Certificate:
    def oid(n: int) -> x509.ObjectIdentifier:
        return x509.ObjectIdentifier(f'1.3.6.1.4.1.57264.1.{n}')

    key: ec.EllipticCurvePrivateKey = ec.generate_private_key(ec.SECP256R1())
    name: x509.Name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'x')])
    now: dt.datetime = dt.datetime.now(dt.timezone.utc)
    builder: x509.CertificateBuilder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(
            x509.UnrecognizedExtension(oid(1), issuer.encode()), critical=False
        )
        .add_extension(
            x509.UnrecognizedExtension(oid(2), trigger), critical=False
        )
        .add_extension(
            x509.UnrecognizedExtension(oid(8), der_string(issuer)),
            critical=False,
        )
    )
    if source is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(oid(12), der_string(source)),
            critical=False,
        )
    return builder.sign(key, hashes.SHA256())


def load_verifier(path: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location('bundle_verifier', path)
    assert spec is not None and spec.loader is not None
    module: types.ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def accepted(policy: typing.Any, cert: x509.Certificate) -> bool:
    try:
        policy.verify(cert)
    except VerificationError:
        return False
    return True


def main() -> None:
    verifier: types.ModuleType = load_verifier(sys.argv[1])
    policy: typing.Any = verifier.ci_policy(REPO)
    provenance: dict[str, typing.Any] = json.loads(
        pathlib.Path(sys.argv[2]).read_text()
    )
    real: str = provenance['attestation_bundles'][0]['attestations'][0][
        'verification_material'
    ]['certificate']
    uri: x509.GeneralName = x509.UniformResourceIdentifier(SIGN_YML)
    cases: dict[str, x509.Certificate] = {
        'real_publish_cert': x509.load_der_x509_certificate(
            base64.b64decode(real)
        ),
        'sign_yml': make_cert([uri]),
        'sign_yml_source_other_case': make_cert(
            [uri], source='https://github.com/wolph/PYTHON-UTILS'
        ),
        'wrong_repo': make_cert(
            [
                x509.UniformResourceIdentifier(
                    SIGN_YML.replace('python-utils', 'numpy-stl')
                )
            ]
        ),
        'wrong_source_repo': make_cert(
            [uri], source='https://github.com/WoLpH/numpy-stl'
        ),
        'no_source_repo': make_cert([uri], source=None),
        'email_san': make_cert([x509.RFC822Name('wolph@wol.ph')]),
        'push_trigger': make_cert([uri], trigger=b'push'),
        'wrong_issuer': make_cert(
            [uri], issuer='https://github.com/login/oauth'
        ),
    }
    result: dict[str, bool] = {}
    for name, cert in cases.items():
        result[name] = accepted(policy, cert)
    print(json.dumps(result))  # noqa: T201


main()
