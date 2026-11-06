"""One home for the ``tomllib`` import.

``tomllib`` joined the standard library in Python 3.11. The package supports
3.10, where the ``tomli`` backport (same API) is installed instead. Importing
``tomllib`` from here keeps that fallback in one place.
"""

try:
    import tomllib  # ty: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # ty: ignore[import-not-found]

__all__ = ['tomllib']
