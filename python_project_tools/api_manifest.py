"""Write the public API of one or more packages as JSON.

Runs inside the interpreter under test, so it uses the standard library
only and works on Python 3.9 and later. The directory given with --path goes first on sys.path, and every
imported package must come from it, so the manifest describes exactly the
tree that was asked for::

    python -I api_manifest.py --path <site-dir> --package python_utils

The manifest lists every public module, and for each module every public
name: what ``__all__`` lists, plus whatever ``dir()`` shows without a
leading underscore. Lazy names behind a module ``__getattr__`` are
resolved, so they are compared like any other name.
"""  # noqa: E501

from __future__ import annotations

import argparse
import enum
import functools
import importlib
import inspect
import json
import pathlib
import pkgutil
import re
import sys
import types

ADDRESS: re.Pattern[str] = re.compile(r' at 0x[0-9a-fA-F]+')
PROTOCOL_DUNDERS: frozenset[str] = frozenset(
    {
        '__call__',
        '__enter__',
        '__exit__',
        '__aenter__',
        '__aexit__',
        '__iter__',
        '__aiter__',
        '__next__',
        '__anext__',
        '__await__',
        '__getitem__',
        '__setitem__',
        '__delitem__',
        '__contains__',
        '__len__',
        '__bool__',
        '__eq__',
        '__hash__',
        '__lt__',
        '__le__',
        '__gt__',
        '__ge__',
        '__add__',
        '__iadd__',
        '__or__',
        '__ior__',
        '__mul__',
        '__imul__',
        '__get__',
        '__set__',
        '__getstate__',
        '__setstate__',
        '__reduce__',
        '__copy__',
        '__deepcopy__',
        '__class_getitem__',
        '__fspath__',
        '__int__',
        '__float__',
        '__index__',
    }
)


def clean_repr(value: object) -> str:
    """Repr without memory addresses, so two runs compare equal."""
    try:
        text: str = repr(value)
    except Exception as error:  # noqa: BLE001 - a broken repr is data here
        text = f'<repr failed: {type(error).__name__}>'
    return ADDRESS.sub('', text)


def describe_signature(obj: object) -> dict[str, object] | None:
    """Parameters and return annotation of a callable, or the reason there are none."""  # noqa: E501
    try:
        signature: inspect.Signature = inspect.signature(obj)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    except (TypeError, ValueError) as error:
        return {'error': f'{type(error).__name__}: {error}'}
    params: list[dict[str, object]] = [
        {
            'name': param.name,
            'kind': param.kind.name,
            'has_default': param.default is not inspect.Parameter.empty,
            'default': None
            if param.default is inspect.Parameter.empty
            else clean_repr(param.default),
        }
        for param in signature.parameters.values()
    ]
    returns: str | None = None
    if signature.return_annotation is not inspect.Signature.empty:
        returns = clean_repr(signature.return_annotation)
    return {'params': params, 'returns': returns}


def qualified_name(obj: object) -> str:
    """module.qualname of a class or function."""
    module: str = getattr(obj, '__module__', None) or '?'
    name: str = (
        getattr(obj, '__qualname__', None)
        or getattr(obj, '__name__', None)
        or '?'
    )
    return f'{module}.{name}'


def member_kind(raw: object) -> str:
    """What a class attribute is, judged from its static value."""
    if raw is None:
        return 'none'
    if isinstance(raw, staticmethod):
        return 'staticmethod'
    if isinstance(raw, classmethod):
        return 'classmethod'
    if isinstance(raw, property):
        return 'property'
    if isinstance(raw, type):
        return 'class'
    if callable(raw):
        return 'method'
    return 'attribute'


def describe_members(cls: type) -> dict[str, dict[str, object]]:
    """Public members and protocol dunders of a class, inherited ones included."""  # noqa: E501
    members: dict[str, dict[str, object]] = {}
    for name in sorted(dir(cls)):
        if name.startswith('_') and name not in PROTOCOL_DUNDERS:
            continue
        try:
            raw: object = inspect.getattr_static(cls, name)
        except AttributeError:
            continue
        if name.startswith('__') and raw is getattr(object, name, None):
            continue
        kind: str = member_kind(raw)
        entry: dict[str, object] = {'kind': kind}
        if kind in (
            'method',
            'classmethod',
            'staticmethod',
        ) and not name.startswith('_'):
            entry['signature'] = describe_signature(getattr(cls, name))
        members[name] = entry
    return members


def describe_class(cls: type, top: str) -> dict[str, object]:
    """Constructor, ancestry, members and enum members of a class."""
    desc: dict[str, object] = {
        'kind': 'class',
        'origin': qualified_name(cls),
        'own': cls.__module__.split('.')[0] == top,
        'signature': describe_signature(cls),
        'bases': [qualified_name(base) for base in cls.__mro__[1:]],
        'members': describe_members(cls),
    }
    if issubclass(cls, enum.Enum):
        names: list[str] = [member.name for member in cls]
        desc['enum'] = names
        desc['members'] = {
            k: v for k, v in desc['members'].items() if k not in names
        }  # type: ignore[union-attr]
    return desc


def is_alias(obj: object) -> bool:
    """True for typing constructs: aliases, unions, TypeVars, special forms."""
    # types.UnionType only exists from Python 3.10.
    alias_types: tuple[type, ...] = (
        types.GenericAlias,
        getattr(types, 'UnionType', types.GenericAlias),
    )
    if isinstance(obj, alias_types):
        return True
    return type(obj).__module__ in (
        'typing',
        'typing_extensions',
    ) and not inspect.isroutine(obj)


def describe_function(obj: object, top: str) -> dict[str, object]:
    """Descriptor of a function or other callable object."""
    module: str = getattr(obj, '__module__', None) or type(obj).__module__
    return {
        'kind': 'function',
        'origin': qualified_name(obj),
        'own': module.split('.')[0] == top,
        'signature': describe_signature(obj),
    }


def is_typing_export(name: str, obj: object) -> bool:
    """True when the object is the typing module's own name, re-exported."""
    for module_name in ('typing', 'typing_extensions', 'collections.abc'):
        module: types.ModuleType | None = sys.modules.get(module_name)
        if module is not None and getattr(module, name, None) is obj:
            return True
    return False


def describe(obj: object, top: str, name: str = '') -> dict[str, object]:
    """Descriptor of one public name."""
    if is_alias(obj):
        own: bool = not is_typing_export(name, obj)
        return {
            'kind': 'alias',
            'origin': qualified_name(type(obj)),
            'own': own,
            'value': clean_repr(obj),
        }
    if isinstance(obj, type):
        return describe_class(obj, top)
    if isinstance(obj, types.ModuleType):
        return {
            'kind': 'module',
            'origin': obj.__name__,
            'own': obj.__name__.split('.')[0] == top,
        }
    if (
        inspect.isroutine(obj)
        or isinstance(obj, functools.partial)
        or callable(obj)
    ):
        return describe_function(obj, top)
    return {
        'kind': 'other',
        'origin': qualified_name(type(obj)),
        'own': True,
        'type': qualified_name(type(obj)),
    }


def public_names(
    module: types.ModuleType,
) -> tuple[list[str] | None, list[str]]:
    """``__all__`` as declared, and every public name the module offers."""
    declared: object = getattr(module, '__all__', None)
    listed: list[str] | None = (
        [str(name) for name in declared]
        if isinstance(declared, (list, tuple))
        else None
    )
    names: set[str] = {
        name for name in dir(module) if not name.startswith('_')
    }
    names.update(listed or [])
    return listed, sorted(names)


def describe_module(module: types.ModuleType, top: str) -> dict[str, object]:
    """Every public name of a module, resolved and described."""
    listed, names = public_names(module)
    described: dict[str, object] = {}
    for name in names:
        try:
            value: object = getattr(module, name)
        except Exception as error:  # noqa: BLE001 - a failing lazy name is a finding
            described[name] = {
                'kind': 'error',
                'origin': '',
                'own': True,
                'error': f'{type(error).__name__}: {error}',
            }
            continue
        described[name] = describe(value, top, name)
    return {'error': None, 'all': listed, 'names': described}


def is_public(dotted: str) -> bool:
    """True when no part of a dotted module name starts with an underscore."""
    return not any(part.startswith('_') for part in dotted.split('.'))


def walk(package: types.ModuleType) -> list[str]:
    """Names of the package and all its public submodules."""
    found: list[str] = [package.__name__]
    search: list[str] = list(getattr(package, '__path__', []))
    for info in pkgutil.walk_packages(
        search, prefix=package.__name__ + '.', onerror=lambda name: None
    ):
        if is_public(info.name):
            found.append(info.name)  # noqa: PERF401
    return sorted(set(found))


def check_origin(module: types.ModuleType, root: pathlib.Path) -> None:
    """Refuse to describe a package that was imported from somewhere else."""
    location: str | None = getattr(module, '__file__', None)
    if (
        location is None
        or root not in pathlib.Path(location).resolve().parents
    ):
        raise SystemExit(
            f'{module.__name__} imported from {location}, not from {root}'
        )


def manifest(packages: list[str], root: pathlib.Path) -> dict[str, object]:
    """Manifest for every package and its public submodules."""
    modules: dict[str, object] = {}
    for top in packages:
        package: types.ModuleType = importlib.import_module(top)
        check_origin(package, root)
        for name in walk(package):
            try:
                module: types.ModuleType = importlib.import_module(name)
            except Exception as error:  # noqa: BLE001 - an import failure is a finding
                modules[name] = {
                    'error': f'{type(error).__name__}: {error}',
                    'all': None,
                    'names': {},
                }
                continue
            modules[name] = describe_module(module, top)
    return {'python': sys.version.split()[0], 'modules': modules}


def main(argv: list[str]) -> int:
    """Parse arguments, describe the packages, print JSON."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0]
    )
    parser.add_argument(
        '--path', required=True, help='directory to import the packages from'
    )
    parser.add_argument(
        '--package',
        action='append',
        required=True,
        help='top-level import name, repeatable',
    )
    args: argparse.Namespace = parser.parse_args(argv)
    root: pathlib.Path = pathlib.Path(args.path).resolve()
    sys.path.insert(0, str(root))
    json.dump(
        manifest(args.package, root), sys.stdout, indent=1, sort_keys=True
    )
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
