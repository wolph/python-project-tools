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
- `conda-forge`: inspect and update the conda-forge feedstock of a package.
