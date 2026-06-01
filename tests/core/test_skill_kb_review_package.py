"""Tests for the skill-kb-review template package.

The three Python step handlers (classify-source, text-review,
human-escalation) are wired into the engine's ``handler_registry`` via
the package's ``register_handlers`` entrypoint. Dispatch routes to
them via the generic ``handler_registry`` lookup in
``dispatch_handler.py``; no feature-specific code exists on the
platform side. The trigger (subscription on ``kb.entry_written``) is
declared in the template YAML.
"""

from __future__ import annotations

import importlib.util
import os
import sys

from carpenter.core.engine import handler_registry, template_manager


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "config_seed", "templates",
)
PKG_DIR = os.path.join(TEMPLATES_DIR, "skill-kb-review")


def _load_pkg_module():
    """Load the package's ``__init__.py`` in isolation.

    Mirrors ``tests/core/test_reflection_per_arc_trigger.py``'s fixture
    for template-package imports — bypasses the real template loader so
    we can assert on the package's own surface without DB side effects.
    """
    pkg_name = "carpenter_template_packages.skill_kb_review_test_fixture"
    init_path = os.path.join(PKG_DIR, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        pkg_name, init_path, submodule_search_locations=[PKG_DIR],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return module


def test_package_directory_layout():
    """Package must be a directory with __init__.py and one YAML."""
    assert os.path.isdir(PKG_DIR), f"expected package dir {PKG_DIR}"
    assert os.path.isfile(os.path.join(PKG_DIR, "__init__.py"))
    assert os.path.isfile(os.path.join(PKG_DIR, "skill-kb-review.yaml"))
    assert os.path.isfile(os.path.join(PKG_DIR, "step_handlers.py"))


def test_register_handlers_registers_all_three_steps():
    """register_handlers must install handlers for each Python-only step."""
    module = _load_pkg_module()
    assert hasattr(module, "register_handlers")

    handler_registry.clear_registry()
    try:
        module.register_handlers(handler_registry)
        registered = set(handler_registry.registered_handlers())
        assert ("skill-kb-review", "classify-source") in registered
        assert ("skill-kb-review", "text-review") in registered
        assert ("skill-kb-review", "human-escalation") in registered
        assert len(registered) == 3
    finally:
        handler_registry.clear_registry()


def test_registered_handlers_are_coroutine_functions():
    """Each registered handler is an async callable with the expected signature."""
    import inspect

    module = _load_pkg_module()
    handler_registry.clear_registry()
    try:
        module.register_handlers(handler_registry)
        for step in ("classify-source", "text-review", "human-escalation"):
            handler = handler_registry.lookup_step_handler(
                "skill-kb-review", step,
            )
            assert handler is not None, f"no handler for {step}"
            assert inspect.iscoroutinefunction(handler), (
                f"handler for {step} is not a coroutine function"
            )
            sig = inspect.signature(handler)
            assert list(sig.parameters) == ["arc_id", "arc_info"]
    finally:
        handler_registry.clear_registry()


def test_template_loads_via_template_manager(tmp_path):
    """Full template loader picks up skill-kb-review from the package dir."""
    import shutil

    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        src = os.path.join(TEMPLATES_DIR, f)
        if os.path.isfile(src) and f.endswith((".yaml", ".yml")):
            shutil.copy(src, dest)
        elif os.path.isdir(src) and not f.startswith((".", "_")):
            shutil.copytree(src, os.path.join(dest, f))

    template_manager.load_templates_from_dir(dest)
    tmpl = template_manager.get_template_by_name("skill-kb-review")
    assert tmpl is not None
    step_names = [s["name"] for s in tmpl["steps"]]
    assert step_names == [
        "classify-source", "text-review", "intent-review", "human-escalation",
    ]
