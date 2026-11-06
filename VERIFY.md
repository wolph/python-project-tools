# Verifying a release

Every release made or backfilled with `ppt` carries signatures next to the files. This page shows how to check them, with nothing but `gpg`, `sha256sum` and `uvx`.

## What is on a release

Each release has the files that PyPI serves for that version, byte for byte, plus these:

- `<file>.asc`: an ASCII armoured detached GPG signature of the file.
- `<file>.sigstore.json`: a Sigstore bundle for the file.
- `SHA256SUMS`: the sha256 of every file on the release.
- `SHA256SUMS.asc`: a GPG signature of `SHA256SUMS`.

Nothing else is added. If a file has no `.asc` next to it, it was not signed.

## Getting the key

The GPG key that signs releases has this fingerprint:

```text
149325FD15904E9C4EB89E95E81444E9CE1F695D
```

Fetch it from a key server:

```bash
gpg --keyserver keys.openpgp.org --recv-keys 149325FD15904E9C4EB89E95E81444E9CE1F695D
```

Or from GitHub, which publishes the keys of an account:

```bash
curl -s https://github.com/wolph.gpg | gpg --import
```

Either way, compare the fingerprint that `gpg --list-keys` prints with the one above before you trust anything. A key you fetched is only as good as that comparison.

## Checking a file

Download the file and its `.asc` into one directory, then verify:

```bash
gpg --verify FILE.asc FILE
```

Look for `Good signature` and the fingerprint above. A warning that the key is not certified is normal if you have not signed it yourself.

To check every file at once, verify the signature on the checksum list first:

```bash
gpg --verify SHA256SUMS.asc SHA256SUMS
```

Then check the files against that list:

```bash
sha256sum -c SHA256SUMS
```

On macOS there is no `sha256sum` by default. Use `shasum -a 256 -c SHA256SUMS` instead.

## Checking a Sigstore bundle signed by Rick

Files signed by hand carry a certificate issued to Rick's GitHub login. Verify with the identity in that certificate and the GitHub OIDC issuer:

```bash
uvx sigstore verify identity --bundle FILE.sigstore.json --cert-identity <identity> --cert-oidc-issuer https://github.com/login/oauth FILE
```

The `<identity>` placeholder gets filled in from the first real bundle, once one exists.

## Checking a bundle signed by CI

Files signed by a GitHub Actions workflow use a different identity, the workflow itself, and a different command. That arrives with the CI signing step and this page will get its own section then.

## Comparing with PyPI

PyPI keeps its own attestations for files uploaded with trusted publishing. Ask for one at:

```text
https://pypi.org/integrity/<name>/<version>/<file>/provenance
```

A 404 means PyPI has none for that file. That is expected for anything uploaded before attestations existed, which is why the signatures on this page exist.

## What "Signed on <date>" means

Backfilled releases end with a footer such as "Signed on 2026-11-06 with ppt sign, after the release". It says the files were signed on that date, not on the day of the release.

The signature therefore proves that the files were the PyPI uploads when they were signed, and that they have not changed since. It does not prove who built them at the time. The Sigstore transparency log records when the bundle was made, so the date in the footer cannot be moved backwards without the log disagreeing.
