2.2.1:

 * ``ppt sign`` regenerates ``SHA256SUMS`` and ``SHA256SUMS.asc`` when the
   signature on the release does not sign the list, as after an upload that
   stopped between the two files, and when the list differs in any byte from
   the one it would write. A list that is not UTF-8 no longer stops
   ``ppt sign --all``. With ``--sigstore-only`` it refuses to write a
   ``SHA256SUMS`` next to an existing ``SHA256SUMS.asc`` and says to rerun
   without that option.
 * ``ppt sign`` signs a version that the PyPI project JSON does not list yet,
   right after an upload, by asking PyPI for that version itself.
 * ``templates/sign.yml`` takes a ``version`` input, and ``ppt sign`` passes
   the PyPI version it matched, so a tag in another form than the version
   (``v1.0-post1`` for ``1.0.post1``) no longer fails the run. A repository
   whose ``sign.yml`` predates the input gets an error saying to copy the
   new template.
 * ``VERIFY.md`` names the footer sentence of a release signed through CI.

2.2.0:

 * ``ppt sign`` takes the Sigstore bundles from the ``sign.yml`` workflow of
   the repository instead of a browser login. It exits 3 when the repository
   has no ``sign.yml`` yet, and ``--sigstore-browser`` signs with the browser
   login as before.
 * ``templates/sign.yml``: the workflow to copy into ``.github/workflows``.
 * ``ppt verify-release`` checks who signed each Sigstore bundle and verifies
   ``SHA256SUMS.asc`` with GPG. Checking that signature needs Rick's public
   key, fingerprint ``149325FD15904E9C4EB89E95E81444E9CE1F695D``, in the
   local keyring.
 * ``VERIFY.md``: how to check a bundle signed by CI.

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
