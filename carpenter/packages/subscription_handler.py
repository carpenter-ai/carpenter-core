"""Work-queue handler for ``package.dispatch`` events.

When a capability package's ``trigger_subscriptions`` entry matches an
event, the subscription action enqueues a ``package.dispatch`` work
item carrying the manifest-declared ``module:func`` handler ref.  This
module's :func:`dispatch_package_handler` is the work-queue handler
that imports the referenced module under the package's isolated
namespace (mirroring :mod:`carpenter.packages.loaders`) and invokes
the handler with the original event payload.

Trust-model notes:

* Package handlers run with the same privileges as the rest of
  package code — they are NOT trusted promoters.  Anything they need
  to do that crosses a U->T boundary must go through the package's
  declared JUDGE pipeline, not a handler return value.
* The handler is given the event payload as a plain dict.  It does
  NOT receive a DB handle, the work_id, or anything that would let it
  modify arc state directly; for that it must call back into the
  platform's existing chat-tool / arc-creation surfaces.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_install_path(package_name: str) -> Path | None:
    """Find the package's install dir from the ``installed_packages`` table.

    Returns ``None`` if the package isn't installed (which usually means
    the work item is stale — we log and bail rather than crashing).
    """
    try:
        from ..db import db_connection
    except ImportError:  # pragma: no cover — defensive
        return None
    try:
        with db_connection() as db:
            row = db.execute(
                "SELECT install_path FROM installed_packages WHERE name = ?",
                (package_name,),
            ).fetchone()
    except Exception:  # noqa: BLE001 — DB may not be ready in tests
        return None
    if row is None:
        return None
    return Path(row[0] if isinstance(row, tuple) else row["install_path"])


async def dispatch_package_handler(work_id: int, payload: dict) -> Any:
    """Work-queue handler for ``package.dispatch`` events.

    Resolves the package's install dir, imports the manifest-declared
    handler under the package's isolated namespace
    (``_carpenter_pkg_.<package>.<module>``), and calls it with the
    original event payload.

    The handler may be a coroutine or a plain function; we ``await``
    coroutines and call sync handlers directly.
    """
    package_name = payload.get("package")
    handler_ref = payload.get("handler")
    if not package_name or not handler_ref:
        logger.warning(
            "package.dispatch work_id=%d missing package/handler in "
            "payload: %s", work_id, payload,
        )
        return None

    install_path = _resolve_install_path(package_name)
    if install_path is None or not install_path.is_dir():
        logger.warning(
            "package.dispatch work_id=%d: package %r not installed "
            "(or install dir missing); dropping",
            work_id, package_name,
        )
        return None

    # Reuse loaders._import_package_module so the package's relative
    # imports work the same way they do for chat tools / JUDGE handlers.
    from .loaders import _import_package_module  # noqa: WPS437 (intentional)

    module_part, _, func_part = str(handler_ref).partition(":")
    if not module_part or not func_part:
        logger.warning(
            "package.dispatch work_id=%d: malformed handler ref %r",
            work_id, handler_ref,
        )
        return None

    try:
        module = _import_package_module(
            package_name, module_part, install_path,
        )
    except ImportError:
        logger.exception(
            "package.dispatch work_id=%d: failed to import %r from "
            "package %r", work_id, module_part, package_name,
        )
        return None

    handler = getattr(module, func_part, None)
    if handler is None or not callable(handler):
        logger.warning(
            "package.dispatch work_id=%d: %s:%s is not callable",
            work_id, module_part, func_part,
        )
        return None

    event_payload = payload.get("event_payload", {})
    try:
        result = handler(event_payload)
        # Allow async handlers.
        if hasattr(result, "__await__"):
            result = await result
        return result
    except Exception:
        logger.exception(
            "package.dispatch work_id=%d: handler %r raised",
            work_id, handler_ref,
        )
        raise
