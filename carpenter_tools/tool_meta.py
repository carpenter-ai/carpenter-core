"""Tool safety declarations and validation.

Each tool function is annotated with @tool() declaring its safety properties.
At startup, the platform validates that folder placement (read/ vs act/) and
declarations agree — two independent signals that must match.
"""


def tool(*, local: bool, readonly: bool, side_effects: bool,
         trusted_output: bool = True,
         param_policies: dict[str, str] | None = None,
         param_types: dict[str, str] | None = None,
         return_types: str | dict[str, str] | None = None):
    """Declare a tool function's safety properties.

    Args:
        local: True if the tool operates only on local data (no network).
        readonly: True if the tool does not modify any state.
        side_effects: True if the tool has observable side effects.
        trusted_output: True if the tool's output can be trusted (no external
            content). False for tools that fetch from untrusted sources
            (e.g., web). Used for conversation taint tracking.
        param_policies: Mapping of parameter name to policy type string
            (e.g., ``{"url": "url", "path": "filepath"}``).  Used by the
            dry-run verifier to check constrained argument *values* against
            the platform allowlist for that policy type (e.g. the "filepath"
            allowlist of permitted paths).  Independent of ``param_types`` —
            both may apply to the same parameter for orthogonal validation:
            ``param_policies`` checks what value is allowed, while
            ``param_types`` checks structural safety constraints.
        param_types: Mapping of parameter name to SecurityType name
            (e.g., ``{"name": "Label", "path": "WorkspacePath"}``).  Used
            by the runtime security-type checker to validate structural
            constraints (e.g. ``WorkspacePath`` ensures the value is
            confined to the agent workspace sandbox).  Independent of
            ``param_policies`` — a parameter may carry both.
        return_types: SecurityType name(s) for return value fields.
            A plain string means the entire return is that type.
            A dict maps field names to types (e.g., ``{"text": "UnstructuredText"}``).

    Usage:
        @tool(local=True, readonly=True, side_effects=False,
              param_types={"path": "WorkspacePath"}, return_types="UnstructuredText")
        def read_file(path: str) -> str: ...
    """
    def decorator(func):
        func._tool_meta = {
            "local": local,
            "readonly": readonly,
            "side_effects": side_effects,
            "trusted_output": trusted_output,
            "param_policies": param_policies,
            "param_types": param_types,
            "return_types": return_types,
        }
        return func
    return decorator


def get_tool_meta(func) -> dict | None:
    """Get the tool metadata from a decorated function, or None."""
    return getattr(func, "_tool_meta", None)


def build_tool_policy_map() -> dict[tuple[str, str, int | str], str]:
    """Build a (module, function, param) -> policy_type map from @tool() metadata.

    Walks ``carpenter_tools.act`` and ``carpenter_tools.read`` via
    :func:`pkgutil.iter_modules`.  For each function that declares
    ``param_policies`` in its ``@tool()`` decorator, emits entries keyed
    by both positional index and keyword name.

    Returns:
        Dict mapping ``(module_leaf, func_name, param_name_or_index)`` to
        the policy type string (e.g. ``"url"``, ``"filepath"``).
    """
    import importlib
    import inspect
    import pkgutil

    result: dict[tuple[str, str, int | str], str] = {}

    for pkg_name in ("carpenter_tools.act", "carpenter_tools.read"):
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
            fqn = f"{pkg_name}.{modname}"
            try:
                mod = importlib.import_module(fqn)
            except ImportError:
                continue
            for attr_name in dir(mod):
                func = getattr(mod, attr_name)
                meta = get_tool_meta(func)
                if meta is None or not meta.get("param_policies"):
                    continue
                sig = inspect.signature(func)
                param_names = list(sig.parameters.keys())
                for param_name, policy_type in meta["param_policies"].items():
                    # Keyword entry
                    result[(modname, attr_name, param_name)] = policy_type
                    # Positional index entry
                    if param_name in param_names:
                        result[(modname, attr_name, param_names.index(param_name))] = policy_type
    return result


def build_tool_type_map() -> dict[tuple[str, str, int | str], str]:
    """Build a (module, function, param) -> SecurityType name map from @tool() metadata.

    Walks ``carpenter_tools.act`` and ``carpenter_tools.read`` via
    :func:`pkgutil.iter_modules`.  For each function that declares
    ``param_types`` in its ``@tool()`` decorator, emits entries keyed
    by both positional index and keyword name.

    Returns:
        Dict mapping ``(module_leaf, func_name, param_name_or_index)`` to
        the SecurityType name (e.g. ``"Label"``, ``"URL"``).
    """
    import importlib
    import inspect
    import pkgutil

    result: dict[tuple[str, str, int | str], str] = {}

    for pkg_name in ("carpenter_tools.act", "carpenter_tools.read"):
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
            fqn = f"{pkg_name}.{modname}"
            try:
                mod = importlib.import_module(fqn)
            except ImportError:
                continue
            for attr_name in dir(mod):
                func = getattr(mod, attr_name)
                meta = get_tool_meta(func)
                if meta is None or not meta.get("param_types"):
                    continue
                sig = inspect.signature(func)
                param_names = list(sig.parameters.keys())
                for param_name, type_name in meta["param_types"].items():
                    # Keyword entry
                    result[(modname, attr_name, param_name)] = type_name
                    # Positional index entry
                    if param_name in param_names:
                        result[(modname, attr_name, param_names.index(param_name))] = type_name
    return result


def build_tool_return_type_map() -> dict[tuple[str, str], str | dict[str, str]]:
    """Build a (module, function) -> return_types map from @tool() metadata.

    Returns:
        Dict mapping ``(module_leaf, func_name)`` to the return_types
        value — either a string or a dict of field names to type names.
    """
    import importlib
    import pkgutil

    result: dict[tuple[str, str], str | dict[str, str]] = {}

    for pkg_name in ("carpenter_tools.act", "carpenter_tools.read"):
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
            fqn = f"{pkg_name}.{modname}"
            try:
                mod = importlib.import_module(fqn)
            except ImportError:
                continue
            for attr_name in dir(mod):
                func = getattr(mod, attr_name)
                meta = get_tool_meta(func)
                if meta is None or meta.get("return_types") is None:
                    continue
                result[(modname, attr_name)] = meta["return_types"]
    return result


def validate_package(package, expected_safe: bool) -> list[str]:
    """Validate all tool functions in a package match expected safety level.

    Args:
        package: The package module (e.g., carpenter_tools.read).
        expected_safe: True for read/ (expect local=True, readonly=True,
                       side_effects=False). False for act/ (expect at least
                       one unsafe property).

    Returns:
        List of error messages. Empty = all valid.
    """
    import importlib
    import pkgutil

    errors = []
    for importer, modname, ispkg in pkgutil.iter_modules(package.__path__):
        mod = importlib.import_module(f"{package.__name__}.{modname}")
        for attr_name in dir(mod):
            func = getattr(mod, attr_name)
            meta = get_tool_meta(func)
            if meta is None:
                continue

            if expected_safe:
                # read/ tools must be local, readonly, no side effects
                if not meta["local"] or not meta["readonly"] or meta["side_effects"]:
                    errors.append(
                        f"{package.__name__}.{modname}.{attr_name}: "
                        f"declared as unsafe but placed in read/ package"
                    )
            else:
                # act/ tools must have at least one unsafe property
                if meta["local"] and meta["readonly"] and not meta["side_effects"]:
                    errors.append(
                        f"{package.__name__}.{modname}.{attr_name}: "
                        f"declared as safe but placed in act/ package"
                    )
    return errors
