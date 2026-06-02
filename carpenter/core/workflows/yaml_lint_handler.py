"""Deterministic YAML-lint handler for the ``yaml-change`` workflow.

Loads every changed YAML file from the arc's workspace via
``yaml.safe_load`` and collects parse-error findings. For files under
``config_seed/templates/*.yaml`` it additionally runs
``carpenter.verify.yaml_template.verify_yaml_template`` so the same
schema/trust-topology checks that protect hand-edited templates also
protect agent-edited ones.

Python-only (I6): no LLM is invoked on the trust boundary. Failure of
any single check fails the step, which fails the arc.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import yaml

from .. import workspace_manager
from ._arc_state import get_arc_state as _get_arc_state, set_arc_state as _set_arc_state
from ..arcs import manager as arc_manager

logger = logging.getLogger(__name__)


_TEMPLATE_REL_PREFIX = os.path.join("config_seed", "templates") + os.sep


def _is_template_yaml(rel_path: str) -> bool:
    """Return True if *rel_path* lives directly under ``config_seed/templates/``."""
    norm = rel_path.replace("\\", "/")
    return norm.startswith("config_seed/templates/") and norm.endswith((".yaml", ".yml"))


def lint_workspace_yaml(workspace_path: str) -> dict[str, Any]:
    """Lint every changed YAML file in *workspace_path*.

    Returns a dict with ``{"ok": bool, "findings": [...], "files": [...]}``.
    Each finding is a ``{"file": str, "severity": str, "message": str}``
    dict. The function never raises on per-file parse errors — they are
    accumulated as findings.
    """
    findings: list[dict[str, Any]] = []
    files_checked: list[str] = []

    try:
        changed = workspace_manager.get_changed_files(workspace_path)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "file": "",
            "severity": "error",
            "message": f"Could not list changed files: {exc}",
        })
        return {"ok": False, "findings": findings, "files": files_checked}

    for rel_path in changed:
        if not rel_path.endswith((".yaml", ".yml")):
            continue
        files_checked.append(rel_path)
        abs_path = os.path.join(workspace_path, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            findings.append({
                "file": rel_path,
                "severity": "error",
                "message": f"Could not read file: {exc}",
            })
            continue
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            findings.append({
                "file": rel_path,
                "severity": "error",
                "message": f"yaml.safe_load failed: {exc}",
            })
            continue
        if _is_template_yaml(rel_path):
            try:
                from ...verify.yaml_template import verify_yaml_template
                result = verify_yaml_template(text)
                if not result.ok:
                    for f in result.findings:
                        findings.append({
                            "file": rel_path,
                            "severity": f.severity,
                            "message": f.message,
                            "line": f.line,
                        })
            except Exception as exc:  # noqa: BLE001 — defensive
                findings.append({
                    "file": rel_path,
                    "severity": "error",
                    "message": f"verify_yaml_template raised: {exc}",
                })

    ok = not any(f.get("severity") == "error" for f in findings)
    return {"ok": ok, "findings": findings, "files": files_checked}


async def handle_lint_yaml(work_id: int, payload: dict) -> None:
    """Work-queue handler for the ``yaml-change.lint-yaml`` step.

    Reads the parent arc's workspace, lints all changed YAML files, and
    records findings to arc_state under ``_lint_yaml_result``.
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("lint-yaml: missing arc_id in payload")
        return

    workspace_path = _get_arc_state(arc_id, "workspace_path")
    if not workspace_path or not os.path.isdir(workspace_path):
        msg = f"workspace_path missing or absent: {workspace_path!r}"
        arc_manager.add_history(arc_id, "lint_yaml_failed", {"message": msg})
        _set_arc_state(arc_id, "_lint_yaml_result", {"ok": False, "findings": [{"message": msg}]})
        try:
            arc_manager.update_status(arc_id, "failed")
        except ValueError:
            pass
        return

    result = lint_workspace_yaml(workspace_path)
    _set_arc_state(arc_id, "_lint_yaml_result", result)
    arc_manager.add_history(
        arc_id,
        "lint_yaml_completed",
        {"ok": result["ok"], "file_count": len(result["files"]), "finding_count": len(result["findings"])},
    )
    if not result["ok"]:
        try:
            arc_manager.update_status(arc_id, "failed")
        except ValueError:
            pass


async def handle_lint_yaml_step(arc_id: int, arc_info: dict) -> None:
    """Step-handler wrapper for the ``yaml-change.lint-yaml`` step (PR 7).

    Called by ``dispatch_handler`` via ``handler_registry`` lookup against
    the (template_name, step_name) pair.  Reads the implementation arc's
    workspace via the verifier arc's ``verification_target_id``, lints
    every changed YAML, records findings on *this* arc, then completes
    + freezes + propagates so the judge sibling can run.
    """
    from ..arcs.dispatch_handler import _propagate_completion

    if arc_info.get("status") == "pending":
        try:
            arc_manager.update_status(arc_id, "active")
        except ValueError:
            pass

    impl_arc_id = arc_info.get("verification_target_id")
    if not impl_arc_id:
        msg = f"lint-yaml arc {arc_id}: no verification_target_id"
        logger.error(msg)
        _set_arc_state(arc_id, "_lint_yaml_result", {"ok": False, "findings": [{"message": msg}]})
        arc_manager.add_history(arc_id, "lint_yaml_failed", {"message": msg})
        try:
            arc_manager.update_status(arc_id, "failed")
        except ValueError:
            pass
        arc_manager.freeze_arc(arc_id)
        _propagate_completion(arc_id)
        return

    workspace_path = _get_arc_state(impl_arc_id, "workspace_path")
    if not workspace_path or not os.path.isdir(workspace_path):
        msg = (
            f"workspace_path missing or absent on impl arc "
            f"{impl_arc_id}: {workspace_path!r}"
        )
        logger.error("lint-yaml arc %d: %s", arc_id, msg)
        _set_arc_state(arc_id, "_lint_yaml_result", {"ok": False, "findings": [{"message": msg}]})
        arc_manager.add_history(arc_id, "lint_yaml_failed", {"message": msg})
        try:
            arc_manager.update_status(arc_id, "failed")
        except ValueError:
            pass
        arc_manager.freeze_arc(arc_id)
        _propagate_completion(arc_id)
        return

    result = lint_workspace_yaml(workspace_path)
    _set_arc_state(arc_id, "_lint_yaml_result", result)
    arc_manager.add_history(
        arc_id,
        "lint_yaml_completed",
        {
            "ok": result["ok"],
            "file_count": len(result["files"]),
            "finding_count": len(result["findings"]),
        },
    )

    try:
        arc_manager.update_status(arc_id, "completed" if result["ok"] else "failed")
    except ValueError:
        pass
    arc_manager.freeze_arc(arc_id)
    _propagate_completion(arc_id)
