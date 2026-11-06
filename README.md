# python-project-tools

`ppt` is the release tooling behind the release-python-package skill. It
checks a package against its last release, verifies a published release and
handles the conda-forge feedstock, so those steps run as tested code instead
of as prose a model has to follow.

## Install

```bash
uv tool install --editable ~/workspace/python-project-tools
```

## Commands

- `check-compat`: compare the public API of a package against its last release.
- `verify-release`: check that a release is complete everywhere it should be.
- `inventory`: list the PyPI versions of a package against GitHub tags, releases and signatures.
- `conda-forge`: inspect and update the conda-forge feedstock of a package.
- `match-tag`: find the commit whose tracked files match a PyPI sdist byte for byte and create a signed tag for it.
- `sign`: sign the PyPI files of a GitHub release with GPG and Sigstore and attach the signatures.
- `backfill`: give every PyPI version a tag, a release and signed files, rebuilding from the tag when PyPI has none.
