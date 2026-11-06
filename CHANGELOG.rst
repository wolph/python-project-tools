2.1.0:

 * ``ppt inventory``: PyPI versions against GitHub tags, releases and
   signatures.
 * ``ppt sign``: GPG and Sigstore signatures for the PyPI files of a release.
 * ``ppt match-tag``: find the commit that matches a PyPI sdist and create a
   signed tag for it.
 * ``ppt backfill``: tag, release and sign every PyPI version.
 * ``ppt verify-release`` checks the signatures (``--skip signatures`` to
   leave it out).
 * ``VERIFY.md``: how to verify a signed release.
 * The shared ``common`` module for subprocess and HTTP access.

2.0.0:

 * Fresh start. Modern package, ``ppt`` CLI, release scripts moved here from
   the release-python-package skill.
