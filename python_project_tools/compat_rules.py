"""Rules that turn two API manifests, or two sets of wheel metadata, into findings.

Pure functions, no I/O, so the rules can be tested on hand-written
manifests. A finding is one of three severities:

* ``break``: code or an install that works with the old release fails
  with the new one. A break needs a major version (a minor one below 1.0).
* ``warn``: a change some users can notice that is not a hard break, such
  as a changed default value or a removed re-export of a foreign name.
* ``add``: something new, which is what a minor version is for.
"""  # noqa: D415, E501

from __future__ import annotations

import dataclasses

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

Desc = dict[str, object]
POSITIONAL: frozenset[str] = frozenset(
    {'POSITIONAL_ONLY', 'POSITIONAL_OR_KEYWORD'}
)
KEYWORD: frozenset[str] = frozenset({'POSITIONAL_OR_KEYWORD', 'KEYWORD_ONLY'})
VARIADIC: frozenset[str] = frozenset({'VAR_POSITIONAL', 'VAR_KEYWORD'})
WIDER_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ('method', 'classmethod'),
        ('method', 'staticmethod'),
    }
)
TYPING_BASES: frozenset[str] = frozenset(
    {
        'typing.Generic',
        'typing.Protocol',
        'typing_extensions.Generic',
        'typing_extensions.Protocol',
    }
)


@dataclasses.dataclass(frozen=True, order=True)
class Finding:
    """One difference between the old and the new release."""

    severity: str
    source: str
    subject: str
    message: str


def params_of(sig: Desc) -> list[Desc]:
    """The parameter list of a described signature."""
    return list(sig.get('params', []))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def positional_findings(
    subject: str, old: list[Desc], new: list[Desc]
) -> list[tuple[str, str]]:
    """Breaks in the order and number of parameters a caller can pass positionally."""  # noqa: E501
    old_pos: list[Desc] = [p for p in old if p['kind'] in POSITIONAL]
    new_pos: list[Desc] = [p for p in new if p['kind'] in POSITIONAL]
    new_star: bool = any(p['kind'] == 'VAR_POSITIONAL' for p in new)
    by_name: dict[str, Desc] = {str(p['name']): p for p in new}
    out: list[tuple[str, str]] = []
    for index, param in enumerate(old_pos):
        name: str = str(param['name'])
        if index >= len(new_pos):
            if new_star:
                continue
            moved: Desc | None = by_name.get(name)
            if moved is not None and moved['kind'] == 'KEYWORD_ONLY':
                out.append(('break', f'`{name}` is now keyword-only'))
            else:
                out.append(
                    (
                        'break',
                        f'positional parameter {index + 1} (`{name}`) removed',
                    )
                )
            continue
        other: Desc = new_pos[index]
        if other['name'] == name:
            continue
        new_names: list[object] = [p['name'] for p in new_pos]
        if name in new_names:
            moved_to: int = new_names.index(name)
            out.append(
                (
                    'break',
                    f'`{name}` moved from position {index + 1} to {moved_to + 1}',  # noqa: E501
                )
            )
        elif param['kind'] == 'POSITIONAL_ONLY':
            out.append(
                (
                    'warn',
                    f'positional-only parameter {index + 1} renamed from `{name}` to `{other["name"]}`',  # noqa: E501
                )
            )
        # A rename of a positional-or-keyword parameter in place still works
        # positionally. keyword_findings reports the keyword break.
    return out


def keyword_findings(
    old: list[Desc], new: list[Desc]
) -> list[tuple[str, str]]:
    """Breaks for callers that pass parameters by name, and default changes."""
    by_name: dict[str, Desc] = {str(p['name']): p for p in new}
    new_kwargs: bool = any(p['kind'] == 'VAR_KEYWORD' for p in new)
    out: list[tuple[str, str]] = []
    for param in old:
        name: str = str(param['name'])
        other: Desc | None = by_name.get(name)
        if (
            param['kind'] in KEYWORD
            and (other is None or other['kind'] not in KEYWORD)
            and not new_kwargs
        ):
            out.append(
                (
                    'break',
                    f'`{name}` can no longer be passed by keyword (removed or renamed)',  # noqa: E501
                )
            )
            continue
        if other is None or param['kind'] in VARIADIC:
            continue
        if param['has_default'] and not other['has_default']:
            out.append(
                ('break', f'`{name}` lost its default and is now required')
            )
        elif param['has_default'] and other['default'] != param['default']:
            out.append(
                (
                    'warn',
                    f'default of `{name}` changed from {param["default"]} to {other["default"]}',  # noqa: E501
                )
            )
    return out


def variadic_findings(
    old: list[Desc], new: list[Desc]
) -> list[tuple[str, str]]:
    """Breaks for removed ``*args`` or ``**kwargs``, and new parameters."""
    out: list[tuple[str, str]] = []
    new_kinds: set[object] = {p['kind'] for p in new}
    for param in old:
        if (
            param['kind'] == 'VAR_POSITIONAL'
            and 'VAR_POSITIONAL' not in new_kinds
        ):
            out.append(('break', f'`*{param["name"]}` removed'))
        elif param['kind'] == 'VAR_KEYWORD' and 'VAR_KEYWORD' not in new_kinds:
            out.append(('break', f'`**{param["name"]}` removed'))
    old_names: set[object] = {p['name'] for p in old}
    old_positional: int = sum(p['kind'] in POSITIONAL for p in old)
    new_positional: list[Desc] = [p for p in new if p['kind'] in POSITIONAL]
    for param in new:
        if param['name'] in old_names or param['kind'] in VARIADIC:
            continue
        # A positional-only parameter in an old slot is a rename, which the
        # positional check already judged. Only extra slots are new.
        if (
            param['kind'] == 'POSITIONAL_ONLY'
            and new_positional.index(param) < old_positional
        ):
            continue
        if param['has_default']:
            out.append(('add', f'new optional parameter `{param["name"]}`'))
        else:
            out.append(('break', f'new required parameter `{param["name"]}`'))
    return out


def diff_signature(
    source: str, subject: str, old: Desc | None, new: Desc | None
) -> list[Finding]:
    """Compare two described signatures of the same callable."""
    if not old or not new or 'error' in old:
        return []
    if 'error' in new:
        return [
            Finding(
                'warn',
                source,
                subject,
                f'signature no longer inspectable: {new["error"]}',
            )
        ]
    old_params: list[Desc] = params_of(old)
    new_params: list[Desc] = params_of(new)
    raw: list[tuple[str, str]] = (
        positional_findings(subject, old_params, new_params)
        + keyword_findings(old_params, new_params)
        + variadic_findings(old_params, new_params)
    )
    return [
        Finding(severity, source, subject, message)
        for severity, message in dict.fromkeys(raw)
    ]


def diff_members(
    source: str, subject: str, old: Desc, new: Desc
) -> list[Finding]:
    """Compare the members of two versions of a class."""
    old_members: dict[str, Desc] = old.get('members', {})  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    new_members: dict[str, Desc] = new.get('members', {})  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    out: list[Finding] = []
    for name, member in old_members.items():
        path: str = f'{subject}.{name}'
        other: Desc | None = new_members.get(name)
        if other is None:
            out.append(Finding('break', source, path, 'member removed'))
            continue
        if other['kind'] != member['kind']:
            severity: str = (
                'warn'
                if (member['kind'], other['kind']) in WIDER_KINDS
                else 'break'
            )
            out.append(
                Finding(
                    severity,
                    source,
                    path,
                    f'changed from {member["kind"]} to {other["kind"]}',
                )
            )
            continue
        out.extend(
            diff_signature(
                source,
                path,
                member.get('signature'),  # ty: ignore[invalid-argument-type]
                other.get('signature'),  # ty: ignore[invalid-argument-type]
            )
        )  # type: ignore[arg-type]
    for name in sorted(set(new_members) - set(old_members)):
        if not name.startswith('_'):
            out.append(  # noqa: PERF401
                Finding('add', source, f'{subject}.{name}', 'new member')
            )
    return out


def diff_class(
    source: str, subject: str, old: Desc, new: Desc
) -> list[Finding]:
    """Compare two versions of a class: constructor, ancestry, members, enum values."""  # noqa: E501
    out: list[Finding] = diff_signature(
        source,
        f'{subject}()',
        old.get('signature'),  # ty: ignore[invalid-argument-type]
        new.get('signature'),  # ty: ignore[invalid-argument-type]
    )  # type: ignore[arg-type]
    old_bases: list[str] = old.get('bases', [])  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    new_bases: set[str] = set(new.get('bases', []))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    for base in old_bases:
        if base in new_bases or base == 'builtins.object':
            continue
        severity: str = 'warn' if base in TYPING_BASES else 'break'
        out.append(
            Finding(
                severity, source, subject, f'no longer a subclass of {base}'
            )
        )
    out.extend(diff_members(source, subject, old, new))
    old_enum: list[str] = old.get('enum', [])  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    new_enum: list[str] = new.get('enum', [])  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    for member in old_enum:
        if member not in new_enum:
            out.append(  # noqa: PERF401
                Finding(
                    'break',
                    source,
                    f'{subject}.{member}',
                    'enum member removed',
                )
            )
    return out


def diff_object(
    source: str, subject: str, old: Desc, new: Desc
) -> list[Finding]:
    """Compare two descriptors of the same public name."""
    old_kind: object = old['kind']
    new_kind: object = new['kind']
    if new_kind == 'error' and old_kind != 'error':
        return [
            Finding(
                'break',
                source,
                subject,
                f'no longer resolves: {new.get("error")}',
            )
        ]
    if old_kind == 'class' and new_kind == 'class':
        return diff_class(source, subject, old, new)
    if old_kind == 'class':
        return [
            Finding(
                'break', source, subject, f'no longer a class, now {new_kind}'
            )
        ]
    if old_kind == 'function' and new_kind in ('function', 'class'):
        out: list[Finding] = diff_signature(
            source,
            subject,
            old.get('signature'),  # ty: ignore[invalid-argument-type]
            new.get('signature'),  # ty: ignore[invalid-argument-type]
        )  # type: ignore[arg-type]
        if new_kind == 'class':
            out.append(
                Finding('warn', source, subject, 'function became a class')
            )
        return out
    if old_kind == 'function' and new_kind != 'alias':
        return [
            Finding(
                'break',
                source,
                subject,
                f'no longer a function, now {new_kind}',
            )
        ]
    if old_kind == 'alias' or new_kind == 'alias':
        if old.get('value') != new.get('value'):
            return [
                Finding(
                    'warn',
                    source,
                    subject,
                    f'type alias changed from {old.get("value") or old_kind} to {new.get("value") or new_kind}',  # noqa: E501
                )
            ]
        return []
    if old_kind != new_kind or old.get('type') != new.get('type'):
        return [
            Finding(
                'warn',
                source,
                subject,
                f'changed from {old.get("type") or old_kind} to {new.get("type") or new_kind}',  # noqa: E501
            )
        ]
    return []


def diff_names(
    source: str, module: str, old: Desc, new: Desc
) -> list[Finding]:
    """Compare the public names of two versions of a module."""
    old_names: dict[str, Desc] = old['names']  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    new_names: dict[str, Desc] = new['names']  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    old_all: set[str] = set(old.get('all') or [])  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    new_all: set[str] = set(new.get('all') or [])  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    out: list[Finding] = []
    for name, desc in old_names.items():
        subject: str = f'{module}.{name}'
        if name not in new_names:
            if desc['kind'] == 'module':
                out.append(
                    Finding(
                        'warn',
                        source,
                        subject,
                        f'module alias for {desc.get("origin")} removed',
                    )
                )
            elif desc.get('own') or name in old_all:
                out.append(Finding('break', source, subject, 'removed'))
            else:
                out.append(
                    Finding(
                        'warn',
                        source,
                        subject,
                        f're-export of {desc.get("origin")} removed',
                    )
                )
            continue
        if (
            name in old_all
            and old.get('all') is not None
            and new.get('all') is not None
            and name not in new_all
        ):
            out.append(
                Finding(
                    'warn',
                    source,
                    subject,
                    'dropped from __all__, so `import *` no longer gets it',
                )
            )
        out.extend(diff_object(source, subject, desc, new_names[name]))
    for name in sorted(set(new_names) - set(old_names)):
        if new_names[name].get('own') or name in new_all:
            out.append(  # noqa: PERF401
                Finding('add', source, f'{module}.{name}', 'new public name')
            )
    return out


def diff_manifests(source: str, old: Desc, new: Desc) -> list[Finding]:
    """Every finding between two API manifests."""
    old_modules: dict[str, Desc] = old['modules']  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    new_modules: dict[str, Desc] = new['modules']  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    out: list[Finding] = []
    for name, module in sorted(old_modules.items()):
        other: Desc | None = new_modules.get(name)
        if module.get('error'):
            out.append(
                Finding(
                    'warn',
                    source,
                    name,
                    f'old release fails to import here: {module["error"]}',
                )
            )
            continue
        if other is None:
            out.append(Finding('break', source, name, 'module removed'))
        elif other.get('error'):
            out.append(
                Finding(
                    'break',
                    source,
                    name,
                    f'module no longer imports: {other["error"]}',
                )
            )
        else:
            out.extend(diff_names(source, name, module, other))
    for name in sorted(set(new_modules) - set(old_modules)):
        out.append(Finding('add', source, name, 'new module'))  # noqa: PERF401
    return out


def supported_pythons(spec: str | None) -> set[str]:
    """The 3.x minor versions a Requires-Python specifier admits."""
    specifier: SpecifierSet = SpecifierSet(spec or '')
    return {
        f'3.{minor}'
        for minor in range(40)
        if specifier.contains(Version(f'3.{minor}'), prereleases=True)
    }


def floor_of(specifier: SpecifierSet) -> Version | None:
    """The lowest version a specifier allows, judged from its lower-bound clauses."""  # noqa: E501
    floors: list[Version] = []
    for clause in specifier:
        if clause.operator in (
            '>=',
            '>',
            '~=',
            '==',
            '===',
        ) and not clause.version.endswith('*'):
            floors.append(Version(clause.version))
        elif clause.operator == '==' and clause.version.endswith('.*'):
            floors.append(Version(clause.version[:-2]))
    return max(floors) if floors else None


def has_ceiling(specifier: SpecifierSet) -> bool:
    """True when a specifier puts an upper limit on the version."""
    return any(
        clause.operator in ('<', '<=', '~=', '==', '===')
        for clause in specifier
    )


def extra_of(requirement: Requirement, extras: list[str]) -> str:
    """The extra a requirement belongs to, '' for the base install."""
    if requirement.marker is None:
        return ''
    for extra in extras:
        if requirement.marker.evaluate(
            {'extra': extra}
        ) and not requirement.marker.evaluate({'extra': ''}):
            return extra
    return ''


def requirements_by_key(
    lines: list[str], extras: list[str]
) -> dict[tuple[str, str, str], Requirement]:
    """Requires-Dist lines keyed by project, extra and environment marker."""
    out: dict[tuple[str, str, str], Requirement] = {}
    for line in lines:
        requirement: Requirement = Requirement(line)
        extra: str = extra_of(requirement, extras)
        marker: str = (
            ''
            if extra or requirement.marker is None
            else str(requirement.marker)
        )
        out[(canonicalize_name(requirement.name), extra, marker)] = requirement
    return out


def requirement_subject(key: tuple[str, str, str]) -> str:
    """Readable name for a dependency key."""
    name, extra, marker = key
    if extra:
        return f'{name} (extra {extra})'
    return f'{name} ({marker})' if marker else name


def diff_requirement(
    key: tuple[str, str, str], old: Requirement, new: Requirement
) -> list[Finding]:
    """Compare the version range of one dependency."""
    subject: str = requirement_subject(key)
    out: list[Finding] = []
    old_floor: Version | None = floor_of(old.specifier)
    new_floor: Version | None = floor_of(new.specifier)
    if new_floor is not None and (old_floor is None or new_floor > old_floor):
        out.append(
            Finding(
                'break',
                'metadata',
                subject,
                f'minimum raised from {old_floor or "none"} to {new_floor}',
            )
        )
    if has_ceiling(new.specifier) and not has_ceiling(old.specifier):
        out.append(
            Finding(
                'warn', 'metadata', subject, f'now capped: {new.specifier}'
            )
        )
    return out


def diff_dependencies(old: Desc, new: Desc) -> list[Finding]:
    """Compare Requires-Dist of two wheels."""
    old_reqs: dict[tuple[str, str, str], Requirement] = requirements_by_key(
        old.get('requires_dist', []),  # ty: ignore[invalid-argument-type]
        old.get('extras', []),  # ty: ignore[invalid-argument-type]
    )  # type: ignore[arg-type]
    new_reqs: dict[tuple[str, str, str], Requirement] = requirements_by_key(
        new.get('requires_dist', []),  # ty: ignore[invalid-argument-type]
        new.get('extras', []),  # ty: ignore[invalid-argument-type]
    )  # type: ignore[arg-type]
    out: list[Finding] = []
    for key, requirement in sorted(new_reqs.items()):
        if key in old_reqs:
            out.extend(diff_requirement(key, old_reqs[key], requirement))
        elif not key[1]:
            out.append(
                Finding(
                    'warn',
                    'metadata',
                    requirement_subject(key),
                    f'new required dependency: {requirement}',
                )
            )
    return out


def diff_entry_points(
    old: dict[str, dict[str, str]], new: dict[str, dict[str, str]]
) -> list[Finding]:
    """Compare console scripts and other entry points."""
    out: list[Finding] = []
    for group, entries in sorted(old.items()):
        for name, target in sorted(entries.items()):
            other: str | None = new.get(group, {}).get(name)
            if other is None:
                out.append(
                    Finding(
                        'break',
                        'metadata',
                        f'{group}:{name}',
                        'entry point removed',
                    )
                )
            elif other != target:
                out.append(
                    Finding(
                        'warn',
                        'metadata',
                        f'{group}:{name}',
                        f'now points to {other}, was {target}',
                    )
                )
    return out


def diff_metadata(old: Desc, new: Desc) -> list[Finding]:
    """Compare wheel metadata: Python support, dependencies, extras, entry points."""  # noqa: E501
    out: list[Finding] = []
    dropped: list[str] = sorted(
        supported_pythons(old.get('requires_python'))  # ty: ignore[invalid-argument-type]
        - supported_pythons(new.get('requires_python')),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        key=lambda version: int(version.split('.')[1]),
    )
    if dropped:
        out.append(
            Finding(
                'break',
                'metadata',
                'Requires-Python',
                f'drops Python {", ".join(dropped)} '
                f'({old.get("requires_python")} -> {new.get("requires_python")})',  # noqa: E501
            )
        )
    out.extend(diff_dependencies(old, new))
    for extra in sorted(
        set(old.get('extras', [])) - set(new.get('extras', []))  # ty: ignore[invalid-argument-type]
    ):  # type: ignore[arg-type]
        out.append(  # noqa: PERF401
            Finding('break', 'metadata', f'extra {extra}', 'extra removed')
        )
    out.extend(
        diff_entry_points(
            old.get('entry_points', {}),  # ty: ignore[invalid-argument-type]
            new.get('entry_points', {}),  # ty: ignore[invalid-argument-type]
        )
    )  # type: ignore[arg-type]
    return out
