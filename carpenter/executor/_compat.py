"""Compatibility shim: carpenter_tools module hierarchy for the restricted executor.

Provides module-like namespace objects that the restricted executor injects so
that code written for the old subprocess executor (``from carpenter_tools.act
import arc; arc.create(...)``) works seamlessly in the RestrictedPython sandbox.

Every function call is routed through the executor's ``dispatch()`` function,
so no real imports or network calls happen from user code.
"""


class _ToolModule:
    """Proxy for a carpenter_tools sub-module (e.g. ``arc``, ``messaging``).

    Attribute access on this object returns a callable that dispatches to the
    tool backend.  For example::

        arc = _ToolModule("arc", dispatch_fn)
        arc.create(name="foo", goal="bar")  # -> dispatch("arc.create", {"name": "foo", "goal": "bar"})
    """

    __slots__ = ("_prefix", "_dispatch")

    def __init__(self, prefix: str, dispatch_fn):
        object.__setattr__(self, "_prefix", prefix)
        object.__setattr__(self, "_dispatch", dispatch_fn)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        prefix = object.__getattribute__(self, "_prefix")
        dispatch = object.__getattribute__(self, "_dispatch")
        tool_name = f"{prefix}.{name}"

        def _call(**kwargs):
            return dispatch(tool_name, kwargs)

        _call.__name__ = name
        _call.__qualname__ = f"{prefix}.{name}"
        return _call


class _PackageNamespace:
    """Proxy for a carpenter_tools package (``act`` or ``read``).

    Attribute access returns a ``_ToolModule`` for the requested sub-module.
    """

    __slots__ = ("_dispatch", "_modules")

    def __init__(self, dispatch_fn):
        object.__setattr__(self, "_dispatch", dispatch_fn)
        object.__setattr__(self, "_modules", {})

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        modules = object.__getattribute__(self, "_modules")
        if name not in modules:
            dispatch = object.__getattribute__(self, "_dispatch")
            modules[name] = _ToolModule(name, dispatch)
        return modules[name]


class _CarpenterToolsRoot:
    """Top-level ``carpenter_tools`` namespace.

    Provides ``.act``, ``.read``, ``.declarations``, and ``.policy``
    sub-packages so that code can import security types at runtime.
    """

    __slots__ = ("_dispatch", "_act", "_read", "_declarations", "_policy")

    def __init__(self, dispatch_fn):
        object.__setattr__(self, "_dispatch", dispatch_fn)
        object.__setattr__(self, "_act", _PackageNamespace(dispatch_fn))
        object.__setattr__(self, "_read", _PackageNamespace(dispatch_fn))
        # Import real declaration and policy types so code can use
        # Label(), URL(), etc. in the restricted executor.
        from carpenter_tools.declarations import (
            SecurityType, Label, Email, URL, WorkspacePath,
            SQL, JSON, UnstructuredText,
        )
        decl_ns = type("declarations", (), {
            "SecurityType": SecurityType,
            "Label": Label,
            "Email": Email,
            "URL": URL,
            "WorkspacePath": WorkspacePath,
            "SQL": SQL,
            "JSON": JSON,
            "UnstructuredText": UnstructuredText,
        })()
        object.__setattr__(self, "_declarations", decl_ns)

        from carpenter_tools.policy.types import (
            EmailPolicy, Domain, Url, FilePath, Command,
            IntRange, Enum, Bool, Pattern, PolicyLiteral,
        )
        policy_ns = type("policy", (), {
            "types": type("types", (), {
                "EmailPolicy": EmailPolicy,
                "Domain": Domain,
                "Url": Url,
                "FilePath": FilePath,
                "Command": Command,
                "IntRange": IntRange,
                "Enum": Enum,
                "Bool": Bool,
                "Pattern": Pattern,
                "PolicyLiteral": PolicyLiteral,
            })(),
        })()
        object.__setattr__(self, "_policy", policy_ns)

    @property
    def act(self):
        return object.__getattribute__(self, "_act")

    @property
    def read(self):
        return object.__getattribute__(self, "_read")

    @property
    def declarations(self):
        return object.__getattribute__(self, "_declarations")

    @property
    def policy(self):
        return object.__getattribute__(self, "_policy")


def build_compat_namespace(dispatch_fn) -> dict:
    """Build the compatibility namespace entries for injection into user code.

    Returns a dict of names to inject into the restricted executor namespace,
    providing carpenter_tools as a pre-imported module hierarchy.

    The entries include:
    - ``carpenter_tools``: The top-level module
    - Individual act modules (``arc``, ``messaging``, ``state``, etc.) for
      direct use without qualifying through carpenter_tools.act

    This allows both patterns:
    - ``carpenter_tools.act.arc.create(name="foo")``
    - ``arc.create(name="foo")``  (pre-imported shorthand)
    """
    root = _CarpenterToolsRoot(dispatch_fn)
    act = root.act

    # Import SecurityType classes for direct use (no import statement needed)
    from carpenter_tools.declarations import (
        Label, Email, URL, WorkspacePath, SQL, JSON, UnstructuredText,
    )

    return {
        "carpenter_tools": root,
        # Pre-import the most commonly used act modules
        "arc": act.arc,
        "messaging": act.messaging,
        "state": act.state,
        "config": act.config,
        "files": act.files,
        "kb": act.kb,
        "scheduling": act.scheduling,
        "web": act.web,
        "git": act.git,
        "platform": act.platform,
        "credentials": act.credentials,
        "conversation": act.conversation,
        "review": act.review,
        "lm": act.lm,
        "webhook": act.webhook,
        "plugin": act.plugin,
        # SecurityType classes for typed string declarations
        "Label": Label,
        "Email": Email,
        "URL": URL,
        "WorkspacePath": WorkspacePath,
        "SQL": SQL,
        "JSON": JSON,
        "UnstructuredText": UnstructuredText,
    }
