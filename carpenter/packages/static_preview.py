"""Static (AST-only) preview of a capability package's chat tools.

The operator's pre-install preview has to answer one question — *what
would installing this package give the chat agent?* — **before** the
operator has consented to installing it.  The obvious implementation is
to import the package's chat-tool modules and read the
``_chat_tool_meta`` the ``@chat_tool`` decorator attaches
(:func:`carpenter.packages.installer.classify_package_chat_tools` does
exactly that).  Importing executes module-level code, so for a source
the operator has not yet agreed to trust that preview *is* the exploit:
arbitrary code runs before the trust prompt it was meant to inform.

This module builds the same preview with :func:`ast.parse` alone.
Nothing here imports, ``exec``s, ``eval``s or compiles the tree, and
nothing it returns depends on the package's code having run.  The cost
is that only *literal* decorator arguments can be read; anything else is
reported in :attr:`StaticPreview.unresolved` and never guessed.

Discovery rules, chosen to match what the runtime actually registers:

* Only functions defined in the module's own scope are considered,
  which includes one guarded by a module-level ``if`` or ``try``.  The
  runtime finds tools with ``dir(module)``, so a decorated function
  nested inside another function or a class body is never a module
  attribute and never registers — such a function is ignored here too.
* A decorator is taken to be ``@chat_tool`` when it is named
  ``chat_tool`` (``@chat_tool``, ``@loader.chat_tool``) or is bound by
  an ``import ... as`` of a name called ``chat_tool``
  (``from ... import chat_tool as ct``).  Matching on the name is
  deliberately fail-open: over-reporting a tool that will not register
  is safe, silently hiding one that will is not.
* The decorator takes no ``name`` argument — the tool name is always the
  decorated function's ``__name__`` (``chat_tool_loader.py``, inside
  ``decorator()``).  So the function name is the tool name, full stop.
* "Write-capable" uses the runtime's own rule: any declared capability
  in :data:`carpenter.chat_tool_registry.WRITE_CAPABILITIES`.  A tool
  whose ``capabilities`` could not be read statically is treated as
  write-capable (fail closed), which is the one deliberate departure.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .manifest import PackageManifest

logger = logging.getLogger(__name__)


#: Positional parameter order of ``chat_tool_loader.chat_tool``.  Kept in
#: this order so a positional application (``@chat_tool("desc", {...})``)
#: reads the same as a keyword one.
_DECORATOR_PARAMS = (
    "description",
    "input_schema",
    "capabilities",
    "trust_boundary",
    "always_available",
    "requires_user_confirm",
)

#: Parameters ``chat_tool`` has no default for.
_DECORATOR_REQUIRED = ("description", "input_schema")

#: The decorator's own attribute/binding name.
_DECORATOR_NAME = "chat_tool"

#: Capability default applied by the decorator when none is declared.
_DEFAULT_CAPABILITIES = ("pure",)

#: Sentinel: the value is declared but is not a literal we can read.
_UNREADABLE = object()

#: The sentence every operator-facing preview must carry.  Exported so
#: the CLI and its tests agree on one wording.
NOT_EXECUTED_NOTICE = (
    "This package's code has NOT been executed. The chat tools below "
    "were read from the source files with a static parse."
)

#: Shown whenever :attr:`StaticPreview.unresolved` is non-empty.
UNRESOLVED_NOTICE = (
    "Because the package's code was not run, the values below could NOT "
    "be determined. They are not guessed: treat this package as "
    "write-capable until you have read the source yourself."
)


@dataclass(frozen=True)
class StaticToolInfo:
    """One ``@chat_tool``-decorated function, as read from the source.

    Attributes:
        name: The decorated function's name, which is the tool name.
        module: The manifest-declared chat-tool module path it lives in.
        description: First line of the declared description, ``""`` when
            it could not be read.
        capabilities: Declared capability strings (the decorator's
            ``["pure"]`` default when the argument is absent).  Empty
            when the argument was present but unreadable — see
            :attr:`capabilities_unresolved`.
        trust_boundary: Declared boundary (``"chat"`` by default), or
            ``""`` when it could not be read.
        always_available: Declared flag; ``False`` when unreadable.
        requires_user_confirm: Declared flag; ``False`` when unreadable.
        write_capabilities: The subset of :attr:`capabilities` in
            ``WRITE_CAPABILITIES``.
        capabilities_unresolved: True when ``capabilities=`` was declared
            with something this module refuses to guess at.  Such a tool
            counts as write-capable.
    """

    name: str
    module: str
    description: str = ""
    capabilities: tuple[str, ...] = ()
    trust_boundary: str = "chat"
    always_available: bool = False
    requires_user_confirm: bool = False
    write_capabilities: tuple[str, ...] = ()
    capabilities_unresolved: bool = False

    @property
    def is_write_capable(self) -> bool:
        """Whether this tool needs the operator's write opt-in.

        The runtime rule (any capability in ``WRITE_CAPABILITIES``), plus
        fail-closed handling of a capability list we could not read.
        """
        return bool(self.write_capabilities) or self.capabilities_unresolved

    @property
    def is_platform_boundary(self) -> bool:
        """Whether the tool declares ``trust_boundary="platform"``.

        Such a tool is hard-refused at registration whatever the
        operator opts into, so it is previewed separately rather than
        grouped with the tools that would register.
        """
        return self.trust_boundary == "platform"

    def as_dict(self) -> dict:
        """Render in the shape ``classify_package_chat_tools`` returns."""
        write_caps = list(self.write_capabilities)
        if self.capabilities_unresolved and not write_caps:
            write_caps = ["<could not be read statically>"]
        return {
            "name": self.name,
            "capabilities": list(self.capabilities),
            "write_capabilities": write_caps,
            "requires_user_confirm": self.requires_user_confirm,
        }


@dataclass(frozen=True)
class StaticPreview:
    """What a static read of the package's chat-tool modules found.

    Attributes:
        tools: Every discovered tool, sorted by name, including any that
            declare the platform trust boundary.
        write_tools: Names of the tools that need the operator's write
            opt-in (chat-boundary and write-capable).
        unresolved: Human-readable notes for everything the static read
            could not settle: a module that is missing, unreadable or
            unparseable, a decorator argument that is not a literal, and
            a declaration whose effect at load time cannot be predicted
            from the source.  Non-empty means the preview is incomplete
            and must say so.
    """

    tools: tuple[StaticToolInfo, ...] = ()
    write_tools: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def registrable_tools(self) -> tuple[StaticToolInfo, ...]:
        """Tools that could register — i.e. not platform-boundary."""
        return tuple(t for t in self.tools if not t.is_platform_boundary)

    @property
    def platform_boundary_tools(self) -> tuple[str, ...]:
        """Names of tools that will be refused for declaring ``platform``."""
        return tuple(t.name for t in self.tools if t.is_platform_boundary)

    @property
    def read_only_tools(self) -> tuple[str, ...]:
        """Names of the tools that register without a write opt-in."""
        return tuple(
            t.name for t in self.registrable_tools if not t.is_write_capable
        )

    def as_classified(self) -> tuple[list[dict], list[dict]]:
        """Return ``(read_only, write_capable)`` dict lists.

        Same shape and same ordering as
        :func:`carpenter.packages.installer.classify_package_chat_tools`,
        so a caller can swap this inspector in without reshaping its
        output.  Platform-boundary tools are omitted from both groups
        exactly as the runtime classifier omits them.
        """
        read_only = [
            t.as_dict() for t in self.registrable_tools
            if not t.is_write_capable
        ]
        write_capable = [
            t.as_dict() for t in self.registrable_tools if t.is_write_capable
        ]
        return read_only, write_capable


def inspect_package_chat_tools(
    pkg_root: Path | str, manifest: PackageManifest,
) -> StaticPreview:
    """Read a package's ``@chat_tool`` declarations without running them.

    Args:
        pkg_root: Directory the package's files live in.  Passed
            explicitly rather than taken from ``manifest.source_path`` so
            a caller previewing a quarantined (e.g. just-fetched) tree
            names the tree it means.
        manifest: The parsed manifest whose ``chat_tools`` list names the
            modules to read.

    Returns:
        A :class:`StaticPreview`.  Never raises for a malformed,
        hostile or missing tree: every such problem lands in
        :attr:`StaticPreview.unresolved` instead, because a preview that
        blows up tells the operator nothing.
    """
    root = Path(pkg_root)
    tools: list[StaticToolInfo] = []
    unresolved: list[str] = []

    for rel in getattr(manifest, "chat_tools", ()) or ():
        _inspect_module(root, str(rel), tools, unresolved)

    tools.sort(key=lambda t: (t.name, t.module))
    write_tools = tuple(sorted({
        t.name for t in tools
        if not t.is_platform_boundary and t.is_write_capable
    }))
    return StaticPreview(
        tools=tuple(tools),
        write_tools=write_tools,
        unresolved=tuple(unresolved),
    )


def write_consent_required(
    preview: StaticPreview, manifest: PackageManifest,
) -> bool:
    """Whether the operator must explicitly opt into write chat tools.

    True when the static read found a write-capable chat tool, and — fail
    closed — also when the package declares chat tools at all but
    something about them could not be read.  A preview that cannot prove
    a package is read-only must not let it install as if it were.
    """
    if preview.write_tools:
        return True
    return bool(preview.unresolved) and bool(
        getattr(manifest, "chat_tools", ()) or (),
    )


# ── module reading ──────────────────────────────────────────────────


def _inspect_module(
    root: Path,
    rel: str,
    tools: list[StaticToolInfo],
    unresolved: list[str],
) -> None:
    """Parse one declared chat-tool module, appending what it declares."""
    rel_path = Path(rel)
    if rel_path.is_absolute():
        unresolved.append(
            f"{rel}: declared as an absolute path, which is refused before "
            f"install; not read",
        )
        return
    try:
        candidate = (root / rel_path).resolve()
        candidate.relative_to(root.resolve())
    except (ValueError, OSError):
        unresolved.append(
            f"{rel}: resolves outside the package root; not read",
        )
        return
    if candidate.suffix != ".py":
        unresolved.append(f"{rel}: is not a .py file; not read")
        return
    if not candidate.is_file():
        unresolved.append(
            f"{rel}: declared in the manifest but is not a file in the "
            f"package; its chat tools could not be determined",
        )
        return
    try:
        source = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        unresolved.append(
            f"{rel}: could not be read as text ({type(exc).__name__}); its "
            f"chat tools could not be determined",
        )
        return
    try:
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        where = getattr(exc, "lineno", None)
        at = f" at line {where}" if where else ""
        unresolved.append(
            f"{rel}: could not be parsed{at} ({type(exc).__name__}); its "
            f"chat tools could not be determined",
        )
        return

    aliases = _decorator_aliases(tree)
    for node in _module_scope_functions(tree):
        info = _read_tool(node, rel, aliases, unresolved)
        if info is not None:
            tools.append(info)


#: Node types that hold statements but do not open a new scope, so a
#: ``def`` inside one is still a module attribute at runtime.
_SCOPED_BODY_HOLDERS = tuple(
    node_type for node_type in (
        ast.stmt,
        ast.excepthandler,
        getattr(ast, "match_case", None),
    ) if node_type is not None
)


def _module_scope_functions(
    tree: ast.Module,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Functions bound in the module's own namespace.

    Descends through module-level ``if`` / ``try`` / ``with`` / loop
    bodies, because a ``def`` in one of those is still a module
    attribute.  Does NOT descend into a function or class body: what it
    defines is not a module attribute, so ``dir(module)`` never sees it
    and the runtime never registers it as a tool.
    """
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    queue = list(ast.iter_child_nodes(tree))
    while queue:
        node = queue.pop(0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(node)
            continue
        if isinstance(node, ast.ClassDef):
            continue
        if isinstance(node, _SCOPED_BODY_HOLDERS):
            queue.extend(ast.iter_child_nodes(node))
    return found


def _decorator_aliases(tree: ast.Module) -> frozenset[str]:
    """Names bound to ``chat_tool`` by an import in this module.

    Any ``from <anywhere> import chat_tool as <alias>`` counts, not only
    an import from ``chat_tool_loader`` itself, so a package that
    re-exports the decorator through a shim module is still read.  The
    cost of being wrong here is naming a tool that will not register,
    which is the safe direction for a security preview.
    """
    aliases = {_DECORATOR_NAME}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name == _DECORATOR_NAME:
                aliases.add(alias.asname or alias.name)
    return frozenset(aliases)


def _is_decorator(node: ast.expr, aliases: frozenset[str]) -> bool:
    """Whether a decorator expression applies ``@chat_tool``."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id in aliases
    if isinstance(target, ast.Attribute):
        return target.attr == _DECORATOR_NAME
    return False


def _read_tool(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    rel: str,
    aliases: frozenset[str],
    unresolved: list[str],
) -> StaticToolInfo | None:
    """Read one function's ``@chat_tool`` declaration, if it has one."""
    index = None
    for i, dec in enumerate(node.decorator_list):
        if _is_decorator(dec, aliases):
            index = i
            break
    if index is None:
        return None
    # decorator_list[0] is the outermost decorator, applied last, so the
    # first @chat_tool found is the one whose metadata survives.
    decorator = node.decorator_list[index]

    # A decorator listed ABOVE @chat_tool runs after it and may return
    # something without the metadata attribute, in which case the tool
    # does not register.  A static read cannot tell.
    outer = [
        _describe_decorator(d) for d in node.decorator_list[:index]
    ]
    if outer:
        unresolved.append(
            f"{rel}: {node.name}() is wrapped by {', '.join(outer)} outside "
            f"@chat_tool, so whether it still registers as a chat tool "
            f"could not be determined",
        )

    values = _read_decorator_values(decorator, rel, node.name, unresolved)
    return _build_tool_info(values, rel, node.name, unresolved)


def _describe_decorator(node: ast.expr) -> str:
    """A short label for a decorator expression, for preview text."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return f"@{target.id}"
    if isinstance(target, ast.Attribute):
        return f"@...{target.attr}"
    return "@<expression>"


def _read_decorator_values(
    decorator: ast.expr,
    rel: str,
    func_name: str,
    unresolved: list[str],
) -> dict[str, Any]:
    """Map ``chat_tool`` parameter names to literal values.

    A parameter whose argument is present but not a literal maps to
    :data:`_UNREADABLE`; an absent one is simply missing from the result.
    """
    if not isinstance(decorator, ast.Call):
        # ``@chat_tool`` without parentheses: nothing is declared, and at
        # import time it raises (the function lands in ``description``).
        unresolved.append(
            f"{rel}: @chat_tool on {func_name}() is applied without "
            f"arguments, so none of its declared values could be read "
            f"(and the package would fail to load)",
        )
        return {name: _UNREADABLE for name in _DECORATOR_PARAMS}

    if any(isinstance(a, ast.Starred) for a in decorator.args) or any(
        kw.arg is None for kw in decorator.keywords
    ):
        unresolved.append(
            f"{rel}: @chat_tool on {func_name}() unpacks its arguments "
            f"(*args / **kwargs), so its declared values could not be read",
        )
        return {name: _UNREADABLE for name in _DECORATOR_PARAMS}

    nodes: dict[str, ast.expr] = {}
    for position, arg in enumerate(decorator.args):
        if position >= len(_DECORATOR_PARAMS):
            unresolved.append(
                f"{rel}: @chat_tool on {func_name}() is given more "
                f"positional arguments than it accepts; the package would "
                f"fail to load",
            )
            break
        nodes[_DECORATOR_PARAMS[position]] = arg
    for keyword in decorator.keywords:
        name = keyword.arg or ""
        if name not in _DECORATOR_PARAMS:
            unresolved.append(
                f"{rel}: @chat_tool on {func_name}() passes unknown "
                f"argument {name!r}; the package would fail to load",
            )
            continue
        nodes[name] = keyword.value

    for name in _DECORATOR_REQUIRED:
        if name not in nodes:
            unresolved.append(
                f"{rel}: @chat_tool on {func_name}() does not pass "
                f"{name!r}; the package would fail to load",
            )

    values: dict[str, Any] = {}
    for name, value_node in nodes.items():
        try:
            values[name] = ast.literal_eval(value_node)
        except (ValueError, TypeError, SyntaxError, MemoryError,
                RecursionError):
            values[name] = _UNREADABLE
            unresolved.append(
                f"{rel}: @chat_tool on {func_name}() declares {name}= as "
                f"something other than a literal ({_shape(value_node)}), so "
                f"its value could not be determined",
            )
    return values


def _is_literal(node: ast.expr) -> bool:
    """Whether ``ast.literal_eval`` can read this node."""
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _shape(node: ast.expr) -> str:
    """Describe why a node is not a literal, without evaluating it."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for element in node.elts:
            if not _is_literal(element):
                return f"a list containing {_shape(element)}"
        return "a non-literal expression"
    if isinstance(node, ast.Dict):
        for element in [*node.keys, *node.values]:
            if element is not None and not _is_literal(element):
                return f"a dict containing {_shape(element)}"
        return "a non-literal expression"
    if isinstance(node, ast.Name):
        return f"the variable {node.id!r}"
    if isinstance(node, ast.Attribute):
        return f"the attribute {node.attr!r}"
    if isinstance(node, ast.Call):
        return "a function call"
    if isinstance(node, ast.JoinedStr):
        return "an f-string"
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                         ast.GeneratorExp)):
        return "a comprehension"
    if isinstance(node, (ast.BinOp, ast.BoolOp, ast.UnaryOp, ast.Compare)):
        return "an expression"
    if isinstance(node, ast.IfExp):
        return "a conditional expression"
    if isinstance(node, ast.Starred):
        return "an unpacking"
    return "a non-literal expression"


def _build_tool_info(
    values: dict[str, Any],
    rel: str,
    func_name: str,
    unresolved: list[str],
) -> StaticToolInfo:
    """Turn read decorator values into a :class:`StaticToolInfo`."""
    from ..chat_tool_registry import (
        VALID_CAPABILITIES,
        WRITE_CAPABILITIES,
        _VALID_BOUNDARIES,
    )

    description = ""
    raw_description = values.get("description")
    if isinstance(raw_description, str):
        lines = raw_description.strip().splitlines()
        description = lines[0].strip() if lines else ""
    elif raw_description is not None and raw_description is not _UNREADABLE:
        unresolved.append(
            f"{rel}: @chat_tool on {func_name}() declares a non-string "
            f"description; the package would fail to load",
        )

    capabilities: tuple[str, ...] = ()
    capabilities_unresolved = False
    if "capabilities" not in values:
        capabilities = _DEFAULT_CAPABILITIES
    else:
        raw = values["capabilities"]
        if raw is _UNREADABLE:
            capabilities_unresolved = True
        elif raw is None:
            capabilities = _DEFAULT_CAPABILITIES
        elif isinstance(raw, (list, tuple)) and all(
            isinstance(c, str) for c in raw
        ):
            capabilities = tuple(raw)
        else:
            capabilities_unresolved = True
            unresolved.append(
                f"{rel}: @chat_tool on {func_name}() declares capabilities= "
                f"as a {type(raw).__name__}, not a list of strings; its "
                f"capabilities could not be determined",
            )

    unknown = [c for c in capabilities if c not in VALID_CAPABILITIES]
    if unknown:
        unresolved.append(
            f"{rel}: @chat_tool on {func_name}() declares unknown "
            f"capabilit{'y' if len(unknown) == 1 else 'ies'} "
            f"{', '.join(repr(c) for c in unknown)}; the package would fail "
            f"to load, so what it would register could not be determined",
        )
    if "pure" in capabilities and len(capabilities) > 1:
        unresolved.append(
            f"{rel}: @chat_tool on {func_name}() mixes 'pure' with other "
            f"capabilities; the package would fail to load, so what it "
            f"would register could not be determined",
        )

    trust_boundary = "chat"
    raw_boundary = values.get("trust_boundary", "chat")
    if raw_boundary is _UNREADABLE:
        trust_boundary = ""
    elif isinstance(raw_boundary, str):
        trust_boundary = raw_boundary
        if raw_boundary not in _VALID_BOUNDARIES:
            unresolved.append(
                f"{rel}: @chat_tool on {func_name}() declares "
                f"trust_boundary={raw_boundary!r}, which is not valid; the "
                f"package would fail to load",
            )
    else:
        trust_boundary = ""
        unresolved.append(
            f"{rel}: @chat_tool on {func_name}() declares a non-string "
            f"trust_boundary; the package would fail to load",
        )

    flags: dict[str, bool] = {}
    for name in ("always_available", "requires_user_confirm"):
        raw_flag = values.get(name, False)
        if raw_flag is _UNREADABLE:
            flags[name] = False
        else:
            flags[name] = bool(raw_flag)

    return StaticToolInfo(
        name=func_name,
        module=rel,
        description=description,
        capabilities=capabilities,
        trust_boundary=trust_boundary,
        always_available=flags["always_available"],
        requires_user_confirm=flags["requires_user_confirm"],
        write_capabilities=tuple(
            c for c in capabilities if c in WRITE_CAPABILITIES
        ),
        capabilities_unresolved=capabilities_unresolved,
    )
