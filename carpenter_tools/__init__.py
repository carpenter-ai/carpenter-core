"""Executor-side tool package for Carpenter.

Tools are partitioned into two subpackages:

- ``read/``  — read-only, local-only tools
- ``act/``   — action tools requiring reviewed Python code

Invocation model
----------------

The modules under ``carpenter_tools.act`` and ``carpenter_tools.read`` are
**declarations**, not runtime implementations.  Function bodies are
``...`` stubs; when reviewed user code runs inside the RestrictedPython
executor, attribute access on these modules is intercepted by the compat
shim in ``carpenter.executor._compat`` which converts every call into
``dispatch("<module>.<func>", kwargs)``.  The dispatch call routes
through ``carpenter.executor.dispatch_bridge.validate_and_dispatch`` to
the real handler in ``carpenter/tool_backends/<tool>.py``.

What these files are consumed for
---------------------------------

- ``carpenter_tools.tool_meta.build_tool_policy_map`` /
  ``build_tool_type_map`` / ``build_tool_return_type_map`` — walk these
  modules at startup and harvest ``@tool(...)`` metadata + signatures.
- ``carpenter.kb.autogen`` — AST-parses the files to generate KB doc
  entries (pulls signatures and docstring first lines).
- ``carpenter.security.trust`` — references the module names as strings
  on its trusted-imports allowlist.
- ``carpenter_tools.declarations`` / ``carpenter_tools.policy.types`` /
  ``carpenter_tools.tool_meta`` are imported directly by server code
  (e.g. ``carpenter.verify``) and carry real runtime behaviour.

The exceptions — modules whose function bodies do run — are the handful
of pure-local utilities that don't need platform state: ``act.files``,
``read.files``, ``read.platform_time``, and ``read.system_info``.
"""
from . import read, act
