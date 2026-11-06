# python-project-tools

`ppt` is the release tooling behind the release-python-package skill. It
checks a package against its last release, verifies a published release and
handles the conda-forge feedstock, so those steps run as tested code instead
of as prose a model has to follow.

## Install

```bash
uv tool install --editable ~/workspace/python-project-tools
```

## Verifying a release

Releases signed with `ppt sign` carry GPG signatures and Sigstore bundles.
[VERIFY.md](https://github.com/WoLpH/python-project-tools/blob/master/VERIFY.md)
shows how to check them.

## Commands

- `check-compat`: compare the public API of a package against its last release.
- `verify-release`: check that a release is complete everywhere it should be.
- `inventory`: list the PyPI versions of a package against GitHub tags, releases and signatures.
- `conda-forge`: inspect and update the conda-forge feedstock of a package.
- `match-tag`: find the commit whose tracked files match a PyPI sdist byte for byte and create a signed tag for it.
- `sign`: sign the PyPI files of a GitHub release with GPG and Sigstore and attach the signatures.
- `backfill`: give every PyPI version a tag, a release and signed files, rebuilding from the tag when PyPI has none.

`ppt backfill --batch FILE` backfills several packages in one run and needs a
single Sigstore browser login for all of them. The file lists one
`PACKAGE CHECKOUT_PATH` pair per line, with an absolute path (`~` is
expanded), and skips blank lines and lines starting with `#`. Each package
runs its checks and releases in turn, then every Sigstore bundle is made in
one go, then each package prints its report and a summary table closes the
run. A package that is refused or stops does not stop the others. The exit
status is the worst of all packages. `--only`, `--repo` and `--checkout`
name one package, so they are refused together with `--batch`.

Sigstore signing runs `python_project_tools/sigstore_signer.py` inside the
pinned `sigstore==4.5.1` environment instead of `sigstore sign`, which signs
every file at once and was rate limited by the public instance (HTTP 429
from Rekor and the timestamp authority) on a batch of 1247 files. The script
signs one file every half second from one login, backs off on 429 and 5xx
answers and writes each bundle as soon as it exists. When the identity token
expires or the rate limit persists it stops, the releases whose bundles are
all there are still finished, and the next run signs only what is missing.
