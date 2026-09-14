"""Name closure, effect folding, string index and whole-module fallback (see
`plans/affected-tests-plan.md`, "What a passing test depends on", rules 2-5): resolves a set of
seed `Block`s -- what a test's collector, or a `module`/`session` fixture's, actually ran -- into
every first-party dependency key reaching those blocks depends on, and separately answers what a
given effect statement folds onto.

Splitting a file's source into `Block`s is `blocks.py`'s job, one parse per file. This module
parses each first-party file a *second* time to build the extra per-file model `Block` doesn't
carry -- where each import-bound name actually comes from, which string literals sit under which
top-level statement, and whether the file trips whole-module fallback -- and otherwise reuses a
`Block`'s own `references`/`effect`/`binds`/`qualname`, so name resolution and effect folding
never diverge from what a `Block`'s checksum already covers. (A cold parse-plus-hash of
`oss/pytest/src`, 39k LOC, took ~0.5s in the Fingerprints probe -- see
`research/affected/reports/fingerprint-probes.md` -- so doubling that per changed file is not a
real cost yet; unifying the two into one parse, if it ever needs to be, is straightforward, since
`World`'s own build already walks `tree.body` in the same order `blocks.parse_blocks` does.)

**Closure vs. effect folding -- two different questions, not one BFS.** `World.closure` answers
"what does this test's own traced code reach", by static reference alone: it never discovers a
new route decorator added in some unrelated file, because nothing in the test's own reachable
code names that statement. What makes that new route matter anyway is the *other* direction --
"`app`'s checksum must incorporate every effect anywhere that targets it" -- which is a property
of the **key**, computed once by whoever hashes it (`store.py`, not built yet), not of any one
test's closure. `World.effect_fold_target` answers that second question for one statement at a
time; `closure` never calls it. This is why the Fingerprints section phrases the rule as "The
checksum covers ... every effect folded onto it from any file", about a key's checksum, while
"depending on anything in a module pulls in [its own imports]" (rule 3's *other* sub-bullet) very
much is a closure-membership rule -- forward-reachable from a module a test already touches,
unlike a stranger's effect statement -- and `closure` does apply it, via `touch()` below.

`Block`'s own docstring explains why an attribute chain `a.b.c` is covered by its root name
alone: "`a` is exactly what a reference to it needs to resolve". That choice is what makes
`import x as m; m.foo()` and `dir(x)` (rule 4's own example) resolve identically here -- both are
just a reference to `m`, indistinguishable without the chain blocks.py deliberately doesn't keep
-- so *any* reference to a name imported as a whole module (`import x`, or `from pkg import mod`
where `mod` is itself a submodule) is treated as depending on that whole module: coarser than the
prototype analyzer (`research/affected/name-deps/`), but sound, and exactly rule 4's own fallback
mechanism rather than a special case bolted onto it.

`code -> block` resolution -- matching a `CollectorRecord`'s `(filename, qualname)` pairs back to
seed keys at session end -- is `World.resolve_code` below, driven per record by `seeds.py`'s
`seeds_for_record`; `closure` here takes already-identified seed blocks, not raw collector output.
Turning the returned keys and fold targets into checksums, and merging several statements under
one `(path, name)` key's hash, is `store.py`'s job, not built either.

Known gaps, matching the plan's own Failure modes table: `importlib.import_module` and other
runtime imports (rule 6) are the tracer's job, not this module's, and aren't reflected here.
Decorator-factory widening (`_widen_decorator_factories`) follows one hop only, and -- since this
module keeps no AST past its own parse, only each `Block`'s `references` -- approximates "what
the factory's body mutates" as "whatever first-party names the factory's own (already known to be
an effect) def block references", rather than walking its body for subscript/attribute stores the
way `research/affected/name-deps/namedeps.py`'s `factory_closure_mutation_targets` does. A
relative import whose level walks past the top of its own package resolves to nothing, silently
-- the same gap that prototype punted on.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from voci._affected.blocks import Block, parse_blocks

__all__ = [
    "DataKey",
    "DefKey",
    "DependencyKey",
    "DirKey",
    "EnvKey",
    "ModuleKey",
    "NameKey",
    "World",
    "dotted_name_for",
]


@dataclass(frozen=True, slots=True)
class DefKey:
    """One def block: `path`'s def (function, method, or nested def) whose `co_qualname` is
    `qualname`."""

    path: Path
    qualname: str


@dataclass(frozen=True, slots=True)
class NameKey:
    """One module-level name: every top-level statement in `path` binding `name`, plus (per
    `World.effect_fold_target`) every effect folded onto it from any file."""

    path: Path
    name: str


@dataclass(frozen=True, slots=True)
class ModuleKey:
    """Resolution crossed into `dotted`, and this `World` has no first-party source for it --
    stdlib, third-party, or simply absent. Rule 7's `module:<dotted>` key; what it resolved to
    (a distribution version, or "absent") is `store.py`'s job, not this module's."""

    dotted: str


@dataclass(frozen=True, slots=True)
class DataKey:
    """A data file the audit hook (`_affected/audit.py`) saw opened in read mode under rootdir --
    M4's `data:<path>` key (Non-code dependencies design section). Never produced by this module's
    own resolution: it comes straight off a `CollectorRecord`'s own `data` set, the way a `DefKey`/
    `NameKey` seed comes off its `codes` (`seeds.py`), and passes through `World.closure` untouched
    -- nothing about a data file's *content* is reachable by static reference the way a def's body
    is."""

    path: Path


@dataclass(frozen=True, slots=True)
class DirKey:
    """A directory the audit hook saw listed (`os.listdir`/`os.scandir`) under rootdir -- M4's
    `dir:<path>` key, checksummed as a hash of its sorted entry names rather than any one file's
    content. Same non-resolved, pass-through-only relationship to `World.closure` as `DataKey`."""

    path: Path


@dataclass(frozen=True, slots=True)
class EnvKey:
    """An environment variable the `os.environ` recorder (`_affected/environ.py`) saw read during
    a test's or fixture's span -- M4's `env:<NAME>` key, checksummed as a hash of its current value
    or "absent". Has no `path`, unlike the other non-code keys: an environment variable isn't
    scoped to any file, so `store.py`'s own per-file `dep_set` grouping gives it a synthetic group
    of its own, the same way `ModuleKey` already does for a dotted name with no file behind it."""

    name: str


DependencyKey = DefKey | NameKey | ModuleKey | DataKey | DirKey | EnvKey


@dataclass(frozen=True, slots=True)
class _WholeModule:
    """Internal only: a reference this module's coarse model can't follow further landed on
    `dotted` as a whole. Never returned to a caller -- `closure` expands it in place into
    `dotted`'s own top-level keys, chased through its own re-exports, the way importing `dotted`
    for real would run its whole top level (rule 4's "expanded through that module's
    re-exports")."""

    dotted: str


_DepItem = DefKey | NameKey | ModuleKey | _WholeModule


@dataclass(frozen=True, slots=True)
class _Import:
    """Where a name a file binds via a top-level `import`/`from ... import` actually comes
    from. `attr` is `None` when `name` is bound to a module object itself -- a plain
    `import pkg.sub [as name]`, or `from pkg import sub` where `sub` is itself a submodule --
    rather than to a name inside it. `module` is `None` only for a relative import whose level
    walks past the top of its own package (a documented gap; see module docstring)."""

    module: str | None
    attr: str | None


@dataclass(slots=True)
class _File:
    path: Path
    dotted: str
    blocks: tuple[Block, ...]
    top_level: tuple[Block, ...]
    raw_nodes: dict[Block, ast.stmt]  # a top-level Block -> the ast.stmt it came from
    class_names: frozenset[str]
    imports: dict[str, _Import]
    nested_imports: dict[str, dict[str, _Import]]  # def qualname -> its own local imports
    strings: dict[str, frozenset[str]]  # a top-level bound name -> string literals under it
    dunder_all: list[str] | None
    module_global: frozenset[str]  # top-level names bound by an Import/ImportFrom statement
    session_global: frozenset[str]  # effect statements with no first-party target at all
    whole_module_reason: str | None

    def top_level_names(self) -> frozenset[str]:
        return frozenset(name for block in self.top_level for name in block.binds)


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_STRING_STOPWORDS = frozenset(
    {
        "get",
        "set",
        "id",
        "name",
        "type",
        "data",
        "value",
        "values",
        "index",
        "key",
        "keys",
        "list",
        "str",
        "int",
        "bool",
        "float",
        "dict",
        "self",
        "cls",
        "args",
        "kwargs",
        "none",
        "true",
        "false",
        "path",
        "url",
        "item",
        "items",
        "result",
        "response",
        "request",
        "config",
        "default",
        "message",
        "status",
        "code",
        "error",
        "test",
        "main",
        "run",
        "start",
        "end",
        "new",
    }
)
_MUTATING_CALLS = frozenset({"exec", "eval", "globals", "vars"})


def dotted_name_for(path: Path, rootdir: Path) -> str:
    """The dotted module name Python's own import system would give `path`: walk up while each
    parent still holds an `__init__.py`, then join from there. No namespace-package handling --
    a directory with no `__init__.py` simply stops the walk, same as `research/affected/
    name-deps/namedeps.py`'s own discovery."""
    path = path.resolve()
    rootdir = rootdir.resolve()
    parts = [path.stem] if path.stem != "__init__" else []
    current = path.parent
    while current != rootdir and (current / "__init__.py").is_file():
        parts.insert(0, current.name)
        current = current.parent
    return ".".join(parts)


class World:
    """Every first-party file resolve.py knows about, keyed by dotted module name, plus the
    resolution engine over it.

    Built once from a batch of `(dotted name, source)` pairs -- so that import resolution can
    tell a package member from a submodule before parsing any file's own imports, the way
    `namedeps.py`'s own two-phase build needed `modules` populated with placeholders first."""

    def __init__(self, files: Mapping[Path, tuple[str, str]]) -> None:
        """`files`: `path -> (dotted, source)` for every first-party file. `dotted` is typically
        `dotted_name_for(path, rootdir)`."""
        self._dotted_for_path: dict[Path, str] = {p: d for p, (d, _s) in files.items()}
        self._path_for_dotted: dict[str, Path] = {d: p for p, (d, _s) in files.items()}
        known = frozenset(self._path_for_dotted)
        parsed = {
            dotted: _parse_file(path, dotted, source, known)
            for path, (dotted, source) in files.items()
        }
        self._files: dict[str, _File] = {dotted: file for dotted, (file, _stars) in parsed.items()}
        for file, star_targets in parsed.values():
            for target in star_targets:
                # An explicit import or a local binding always wins over a star-imported name --
                # matches ordinary Python shadowing, and `_star_import_aliases` doesn't know
                # which statement runs last.
                local = file.top_level_names()
                for name, source in self._star_import_aliases(target).items():
                    if name not in file.imports and name not in local:
                        file.imports[name] = source
        self._string_index: dict[str, list[tuple[str, str]]] = {}
        for file in self._files.values():
            for name in file.top_level_names():
                self._string_index.setdefault(name, []).append((file.dotted, name))
        self._cache: dict[tuple[str, str, str | None, bool], frozenset[_DepItem]] = {}
        self._resolving: set[tuple[str, str, str | None, bool]] = set()
        self._resolve_code_cache: dict[tuple[Path, str], frozenset[DefKey | NameKey]] = {}

    def _file(self, dotted: str) -> _File | None:
        return self._files.get(dotted)

    def _star_import_aliases(self, target: str) -> dict[str, _Import]:
        """`from target import *`: precise, not a whole-module fallback -- binds each of
        `target`'s own public names (`__all__` if it declares one, else its non-underscore
        top-level names) as individual aliases, so a re-export hub reached only through a star
        import still only pulls in the one name actually used."""
        source = self._file(target)
        if source is None:
            return {}
        public = (
            source.dunder_all
            if source.dunder_all is not None
            else [name for name in source.top_level_names() if not name.startswith("_")]
        )
        return {name: _Import(module=target, attr=name) for name in public}

    # -- binding resolution ----------------------------------------------

    def _resolve_binding(
        self,
        dotted: str,
        name: str,
        *,
        qualname: str | None = None,
        ignore_fallback: bool = False,
    ) -> frozenset[_DepItem]:
        cache_key = (dotted, name, qualname, ignore_fallback)
        if cache_key in self._cache:
            return self._cache[cache_key]
        if cache_key in self._resolving:
            return frozenset()  # import cycle guard
        self._resolving.add(cache_key)
        try:
            result = self._resolve_binding_uncached(
                dotted, name, qualname=qualname, ignore_fallback=ignore_fallback
            )
        finally:
            self._resolving.discard(cache_key)
        self._cache[cache_key] = result
        return result

    def _resolve_binding_uncached(
        self, dotted: str, name: str, *, qualname: str | None, ignore_fallback: bool
    ) -> frozenset[_DepItem]:
        file = self._file(dotted)
        if file is None:
            return frozenset({ModuleKey(dotted)})
        if file.whole_module_reason is not None and not ignore_fallback:
            return frozenset({_WholeModule(dotted)})
        # Rule 2: "imports inside a function body are references of that def block" -- a nested
        # import's own binding is scoped to the def it's in, so it shadows a same-named
        # module-level import or binding, the same way it would at runtime.
        if qualname is not None and name in file.nested_imports.get(qualname, {}):
            return self._resolve_import(file.nested_imports[qualname][name])
        if name in file.imports:
            return self._resolve_import(file.imports[name])
        if name not in file.top_level_names():
            return frozenset()  # not bound in this module at all
        return self._local_binding_keys(file, name)

    def _resolve_import(self, imp: _Import) -> frozenset[_DepItem]:
        if imp.module is None:
            return frozenset()  # relative import past the package root -- documented gap
        if imp.attr is None:
            if self._file(imp.module) is not None:
                return frozenset({_WholeModule(imp.module)})
            return frozenset({ModuleKey(imp.module)})
        return self._resolve_binding(imp.module, imp.attr)

    def _local_binding_keys(self, file: _File, name: str) -> frozenset[_DepItem]:
        items: set[_DepItem] = {NameKey(file.path, name)}
        if name not in file.class_names:
            for block in file.blocks:
                if block.qualname == name:
                    items.add(DefKey(file.path, name))
                    break
        return frozenset(items)

    def _resolve_refs(
        self, dotted: str, references: Iterable[str], *, qualname: str | None = None
    ) -> set[_DepItem]:
        file = self._file(dotted)
        if file is None:
            return set()
        if file.whole_module_reason is not None:
            return {_WholeModule(dotted)}
        out: set[_DepItem] = set()
        for name in references:
            out |= self._resolve_binding(dotted, name, qualname=qualname)
        return out

    def _resolve_strings(self, strings: Iterable[str]) -> set[_DepItem]:
        out: set[_DepItem] = set()
        for s in strings:
            if not _IDENT_RE.match(s):
                continue
            segment = s.rsplit(".", 1)[-1]
            if len(segment) < 3 or segment.lower() in _STRING_STOPWORDS:
                continue
            for dotted, name in self._string_index.get(segment, ()):
                out |= self._resolve_binding(dotted, name)
        return out

    # -- code -> block resolution (M2's "at session end" bullet) ----------

    def resolve_code(self, path: Path, qualname: str) -> frozenset[DefKey | NameKey]:
        """Maps one `CollectorRecord` code identifier -- `(path, qualname)`, `Collector.finish`'s
        own reduction of a traced `CodeType` -- to the seed keys it stands for, per the
        Fingerprints "Mapping a code object to a block" rule. `path` unknown to this `World` (a
        file this run touched that no longer exists in the tree `World` was built from) seeds
        nothing; whatever else in the closure needed that file already carries a `ModuleKey` for
        it through the ordinary import-resolution path, which doesn't go through here.

        A def's own `co_qualname` matches a `DefKey` directly -- one lookup, not a search keyed
        by identity, since `DefKey` is `(path, qualname)` already. A class body's `co_qualname` is
        the class's own dotted path (`"Outer"`, `"Outer.Inner"` for a nested class); unlike a def
        it has no dedicated `Block` of its own (blocks.py's own docstring: only a *top-level*
        class gets one), so it resolves through its outermost segment to that top-level class's
        `NameKey` instead. Everything else -- `<lambda>`, `<genexpr>`, 3.14's `__annotate__`,
        `<generic parameters of ...>`, and `<module>` itself -- would need the `co_firstlineno`
        `Collector.finish`'s reduction already discarded to locate the one block it belongs to;
        rather than guess, this depends on every name the file binds at its own top level instead,
        the same fail-safe posture rule 4 uses for a reference this module's static model can't
        follow. For `<module>` this is exactly the outcome wanted: it fires once, when the file's
        own import actually ran, and seeding any of its top-level keys is what makes `closure`'s
        own per-module `touch()` pull in that file's `module_global`/`session_global` effects
        (see `closure`'s own docstring) -- without needing a `<module>`-specific case at all.

        Cached by `(path, qualname)`: a shared helper many tests trace would otherwise repeat the
        same block scan, and for the whole-file fallback case, the same per-name walk over every
        top-level binding, once per test rather than once per distinct code identifier.
        """
        cache_key = (path, qualname)
        if cache_key in self._resolve_code_cache:
            return self._resolve_code_cache[cache_key]
        result = self._resolve_code_uncached(path, qualname)
        self._resolve_code_cache[cache_key] = result
        return result

    def _resolve_code_uncached(self, path: Path, qualname: str) -> frozenset[DefKey | NameKey]:
        dotted = self._dotted_for_path.get(path)
        if dotted is None:
            return frozenset()
        file = self._file(dotted)
        if file is None:
            return frozenset()
        def_key = DefKey(path, qualname)
        if _find_blocks(file, def_key):
            return frozenset({def_key})
        first_segment = qualname.split(".", 1)[0]
        if first_segment in file.class_names:
            return frozenset({NameKey(path, first_segment)})
        seeds: set[DefKey | NameKey] = set()
        for name in file.top_level_names():
            seeds |= {
                item
                for item in self._local_binding_keys(file, name)
                if isinstance(item, (DefKey, NameKey))
            }
        return frozenset(seeds)

    # -- closure (rules 2, 4, 5) ------------------------------------------

    def closure(self, seeds: Iterable[DefKey | NameKey]) -> frozenset[DependencyKey]:
        """The transitive dependency closure of `seeds` -- def blocks a collector recorded, plus
        any statement blocks known to matter on their own (a test's own, holding its parametrize
        decorators; see the plan's rule 1). Never returns a whole-module placeholder: reaching
        one immediately expands into `dotted`'s own top-level keys, chased through its own
        re-exports (rule 4)."""
        result: set[_DepItem] = set()
        touched: set[str] = set()
        queue: list[_DepItem] = list(seeds)
        while queue:
            item = queue.pop()
            if item in result:
                continue
            if isinstance(item, _WholeModule):
                queue.extend(self._expand_whole_module(item.dotted))
                continue
            result.add(item)
            queue.extend(self._continuation(item, touched))
        return frozenset(item for item in result if isinstance(item, (DefKey, NameKey, ModuleKey)))

    def _continuation(self, item: DefKey | NameKey | ModuleKey, touched: set[str]) -> set[_DepItem]:
        """Everything `item`'s own presence in the closure adds beyond itself: its block's
        references and string literals (rules 2 and 5), its module's own imports and
        session-global effects the first time that module is touched at all (rule 3's
        module-touch sub-bullet -- see `closure`'s own docstring), and, for a method, its
        top-level class's statement block (rule 2)."""
        if isinstance(item, ModuleKey):
            return set()
        dotted = self._dotted_for_path.get(item.path)
        if dotted is None:
            return set()
        file = self._file(dotted)
        if file is None:
            return set()
        out: set[_DepItem] = set()
        if dotted not in touched:
            touched.add(dotted)
            out |= {NameKey(file.path, name) for name in file.module_global | file.session_global}
        qualname = item.qualname if isinstance(item, DefKey) else None
        for block in _find_blocks(file, item):
            out |= self._resolve_refs(dotted, block.references, qualname=qualname)
        out |= self._resolve_strings(file.strings.get(_own_name(item), frozenset()))
        if isinstance(item, DefKey):
            first_segment = item.qualname.split(".", 1)[0]
            if first_segment in file.class_names:
                # Rule 2: "a method that ran pulls in its class block" -- only for a *top-level*
                # class, whose bases/decorators live in their own statement block; a nested
                # class's are embedded in whichever enclosing def already holds them (see
                # blocks.py's own docstring), reached independently if that def itself ran.
                out.add(NameKey(file.path, first_segment))
        return out

    def _expand_whole_module(self, dotted: str) -> set[_DepItem]:
        file = self._file(dotted)
        if file is None:
            return {ModuleKey(dotted)}
        out: set[_DepItem] = set()
        for name in file.top_level_names() | frozenset(file.imports):
            out |= self._resolve_binding(dotted, name, ignore_fallback=True)
        return out

    # -- effect folding (rule 3) -------------------------------------------

    def effect_fold_target(self, path: Path, block: Block) -> frozenset[DefKey | NameKey]:
        """What `block` -- an effect statement in `path` -- folds onto: every first-party key its
        own references resolve to, widened one hop through a decorator-factory call (see module
        docstring). Empty for a non-effect block, and for a *session-global* effect: one whose
        references resolve to nothing first-party at all (`logging.basicConfig()`, where
        `logging` resolves only to a `ModuleKey`, never a `DefKey`/`NameKey`) or whose target
        module can't be told apart from the effect statement's own import to begin with.
        `closure`'s own `touch()` already covers a *named* session-global effect through
        `session_global`, not through this method, since nothing "targeted" it in the first
        place; an unnamed one (a bare `logging.basicConfig()` with nothing to key it by) is a
        known gap -- see `_session_global_names`.

        A whole-module fallback target expands the same way `closure` would: onto every name
        that module binds, still excluding any that themselves resolve no further than a
        `ModuleKey`."""
        if not block.effect:
            return frozenset()
        dotted = self._dotted_for_path.get(path)
        if dotted is None:
            return frozenset()
        file = self._file(dotted)
        if file is None:
            return frozenset()
        targets = self._resolve_refs(dotted, block.references)
        targets |= _widen_decorator_factories(self, file, block)
        out: set[_DepItem] = set()
        for target in targets:
            if isinstance(target, _WholeModule):
                out |= self._expand_whole_module(target.dotted)
            else:
                out.add(target)
        return frozenset(item for item in out if isinstance(item, (DefKey, NameKey)))


# -- helpers over a resolved key ------------------------------------------------------------


def _own_name(item: DefKey | NameKey) -> str:
    return item.name if isinstance(item, NameKey) else item.qualname.split(".", 1)[0]


def _find_blocks(file: _File, item: DefKey | NameKey) -> list[Block]:
    """Every block `item` names -- not just the first. A `NameKey`'s own docstring promises
    "every top-level statement ... binding `name`" (an `if`/`else` reassignment binds the same
    name from two separate statements), and a `DefKey`'s qualname isn't unique either: an
    `if`/`else` def shares one qualname across two distinct def blocks (see blocks.py's own
    docstring), each with its own references."""
    if isinstance(item, DefKey):
        return [block for block in file.blocks if block.qualname == item.qualname]
    return [block for block in file.top_level if item.name in block.binds]


# -- decorator-factory widening (rule 3's registry heuristic) ------------------------------------


def _widen_decorator_factories(world: World, file: _File, block: Block) -> set[_DepItem]:
    """One-hop interprocedural widening for `@register("key") def handler(): ...` populating a
    module-global registry through the factory's own closure: the decorator's direct reference
    is to `register`, not to `REGISTRY`, so folding this effect onto "whatever it references"
    alone misses it -- the plan's Decisions note the probe's `@register("key")` case was unsound
    without this."""
    node = file.raw_nodes.get(block)
    if node is None:
        return set()
    out: set[_DepItem] = set()
    for call in _decorator_calls(node):
        if not isinstance(call.func, ast.Name):
            continue
        for target in world._resolve_binding(file.dotted, call.func.id):
            if not isinstance(target, DefKey):
                continue
            factory_dotted = world._dotted_for_path.get(target.path)
            if factory_dotted is None:
                continue
            factory_file = world._file(factory_dotted)
            if factory_file is None:
                continue
            for mutated in _factory_body_references(factory_file, target.qualname):
                out |= world._resolve_binding(factory_dotted, mutated)
    return out


def _factory_body_references(factory_file: _File, qualname: str) -> set[str]:
    """Every name the factory's own body references -- including a nested def's, since
    `blocks.py` carves each nested def (`register.<locals>.decorator`) into its own block, so
    `register`'s own def block never sees `REGISTRY[key] = fn` directly; that mutation lives in
    the nested block instead. A def block's `effect` is always `False` (only a statement block
    carries that flag), so this can't filter on it -- every reference in scope is a candidate,
    the same coarseness `resolve_binding` itself already applies everywhere else."""
    prefix = f"{qualname}."
    out: set[str] = set()
    for candidate in factory_file.blocks:
        if candidate.qualname == qualname or (
            candidate.qualname is not None and candidate.qualname.startswith(prefix)
        ):
            out |= candidate.references
    return out


def _decorator_calls(node: ast.stmt) -> list[ast.Call]:
    decorators: list[ast.expr] = list(getattr(node, "decorator_list", []) or [])
    for sub in ast.walk(node):
        if sub is node:
            continue
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            decorators.extend(sub.decorator_list)
    return [d for d in decorators if isinstance(d, ast.Call)]


# -- per-file model, built from a single extra `ast.parse` ---------------------------------------


@dataclass
class _Scan:
    """Accumulator for `_scan_top_level`'s single pass -- everything `_File` needs beyond
    `blocks.parse_blocks`'s own output."""

    class_names: set[str] = field(default_factory=set)
    imports: dict[str, _Import] = field(default_factory=dict)
    strings: dict[str, frozenset[str]] = field(default_factory=dict)
    dunder_all: list[str] | None = None
    module_global: set[str] = field(default_factory=set)
    whole_module_reason: str | None = None
    raw_nodes: dict[Block, ast.stmt] = field(default_factory=dict)
    star_targets: list[str] = field(default_factory=list)


def _scan_statement(
    scan: _Scan, stmt: ast.stmt, block: Block, dotted: str, known: frozenset[str]
) -> None:
    scan.raw_nodes[block] = stmt
    if isinstance(stmt, ast.ClassDef):
        scan.class_names.add(stmt.name)
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        scan.imports.update(_import_sources(stmt, dotted, known))
        scan.module_global.update(block.binds)
        star_target = _star_import_target(stmt, dotted)
        if star_target is not None:
            scan.star_targets.append(star_target)
    literals = _string_literals(stmt) if block.binds else frozenset()
    for name in block.binds:
        scan.strings[name] = literals
    values = _dunder_all_values(stmt)
    if values is not None:
        scan.dunder_all = values
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "__getattr__":
        scan.whole_module_reason = "module __getattr__ (PEP 562)"
    if _has_module_level_dynamic_eval(stmt):
        scan.whole_module_reason = "exec()/eval()/globals()/vars() at module level"


def _scan_top_level(
    tree: ast.Module, top_level: tuple[Block, ...], dotted: str, known: frozenset[str]
) -> _Scan:
    """Rule 5's "string constants assigned in class bodies are indexed too" needs no separate
    pass: `_scan_statement`'s own `_string_literals(stmt)` already walks a class's *whole*
    subtree -- resolve.py's own raw, uncarved node, not `blocks.py`'s carved stub -- so a
    class-body string is already indexed under the class's own name."""
    scan = _Scan()
    for stmt, block in zip(tree.body, top_level, strict=True):
        _scan_statement(scan, stmt, block, dotted, known)
    return scan


def _parse_file(
    path: Path, dotted: str, source: str, known: frozenset[str]
) -> tuple[_File, list[str]]:
    tree = ast.parse(source, filename=str(path))
    blocks = tuple(parse_blocks(source, str(path)))
    top_level = tuple(b for b in blocks if b.qualname is None)
    assert len(top_level) == len(tree.body), (
        "blocks.py produces exactly one statement block per top-level statement, in order -- "
        "see its module docstring"
    )

    scan = _scan_top_level(tree, top_level, dotted, known)
    session_global = _session_global_names(top_level, scan.imports, known)

    file = _File(
        path=path,
        dotted=dotted,
        blocks=blocks,
        top_level=top_level,
        raw_nodes=scan.raw_nodes,
        class_names=frozenset(scan.class_names),
        imports=scan.imports,
        nested_imports=_nested_imports(tree, dotted, known),
        strings=scan.strings,
        dunder_all=scan.dunder_all,
        module_global=frozenset(scan.module_global),
        session_global=frozenset(session_global),
        whole_module_reason=scan.whole_module_reason,
    )
    return file, scan.star_targets


def _nested_imports(
    tree: ast.Module, dotted: str, known: frozenset[str]
) -> dict[str, dict[str, _Import]]:
    """Rule 2: "imports inside a function body are references of that def block". `blocks.py`'s
    `_collect_references` already puts a nested import's bound name into its def block's
    `references`; this builds the other half, where that name actually resolves to, keyed by
    the enclosing def's own qualname (built the same way `blocks.py`'s carving does: a nested
    `def`'s own qualname joins its enclosing scope, and its *body* is walked under that scope
    plus a trailing `<locals>`). A class body isn't its own scope here -- a bare `import` inside
    one is rare, and blocks.py doesn't carve a class body into a separate reference scope for
    imports either, so it stays attributed to whichever def (if any) encloses the class."""
    out: dict[str, dict[str, _Import]] = {}

    def visit(node: ast.AST, qualname: str | None, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner_qualname = ".".join((*scope, child.name))
                visit(child, inner_qualname, (*scope, child.name, "<locals>"))
            elif isinstance(child, ast.ClassDef):
                visit(child, qualname, (*scope, child.name))
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                if qualname is not None:
                    out.setdefault(qualname, {}).update(_import_sources(child, dotted, known))
            else:
                visit(child, qualname, scope)

    visit(tree, None, ())
    return out


def _session_global_names(
    top_level: tuple[Block, ...], imports: dict[str, _Import], known: frozenset[str]
) -> set[str]:
    """Names of effect statements whose references resolve to nothing first-party at all --
    process-wide setup like `logging.basicConfig()` (rule 3's session-global effects). A
    reference that resolves to *something* first-party, even a whole-module fallback,
    disqualifies a statement from this bucket; it's an ordinary effect fold instead."""
    out: set[str] = set()
    for block in top_level:
        if not block.effect or not block.binds:
            continue
        if any(_names_first_party(ref, imports, known) for ref in block.references):
            continue
        out.add(next(iter(sorted(block.binds))))
    return out


def _names_first_party(name: str, imports: dict[str, _Import], known: frozenset[str]) -> bool:
    if name in imports:
        module = imports[name].module
        return module is not None and module in known
    return True  # a plain first-party-bound name (not an import alias) always counts


def _import_sources(
    stmt: ast.Import | ast.ImportFrom, dotted: str, known: frozenset[str]
) -> dict[str, _Import]:
    out: dict[str, _Import] = {}
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            if alias.asname:
                out[alias.asname] = _Import(module=alias.name, attr=None)
            else:
                top = alias.name.split(".")[0]
                out[top] = _Import(module=top, attr=None)
        return out
    src = _resolve_relative(dotted, stmt.level, stmt.module)
    if src is None:
        for alias in stmt.names:
            if alias.name != "*":
                out[alias.asname or alias.name] = _Import(module=None, attr=None)
        return out
    for alias in stmt.names:
        if alias.name == "*":
            continue  # star imports: see `_star_imports`'s own gap note
        bound = alias.asname or alias.name
        candidate = f"{src}.{alias.name}"
        if candidate in known:
            out[bound] = _Import(module=candidate, attr=None)
        else:
            out[bound] = _Import(module=src, attr=alias.name)
    return out


def _star_import_target(stmt: ast.stmt, dotted: str) -> str | None:
    if not isinstance(stmt, ast.ImportFrom) or not any(a.name == "*" for a in stmt.names):
        return None
    return _resolve_relative(dotted, stmt.level, stmt.module)


def _dunder_all_values(stmt: ast.stmt) -> list[str] | None:
    if not (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and stmt.targets[0].id == "__all__"
        and isinstance(stmt.value, (ast.List, ast.Tuple, ast.Set))
    ):
        return None
    return [
        elt.value
        for elt in stmt.value.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def _resolve_relative(dotted: str, level: int, module: str | None) -> str | None:
    if level == 0:
        return module
    parts = dotted.split(".")
    # `dotted` names a module, not a package, so its own package is everything but its last
    # segment; one further `.` per extra level of leading dots in the import.
    base_parts = parts[:-1]
    if level > 1:
        cut = level - 1
        if cut > len(base_parts):
            return None
        base_parts = base_parts[:-cut] if cut else base_parts
    base = ".".join(base_parts)
    if module:
        return f"{base}.{module}" if base else module
    return base or None


def _has_module_level_dynamic_eval(stmt: ast.stmt) -> bool:
    """Whether `exec`/`eval`/`globals`/`vars` is called directly in `stmt`, not inside a nested
    def -- a nested def's own body is a different block, and calling it later doesn't make the
    *module* untrustworthy to resolve (see the analogous check in `research/affected/name-deps/
    namedeps.py`)."""
    for node in ast.walk(stmt):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not stmt:
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _MUTATING_CALLS
        ):
            return True
    return False


def _string_literals(node: ast.AST) -> frozenset[str]:
    return frozenset(
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
