"""``python3 -m carpenter budget`` — operator control of the API budget breaker.

The operator's own path to the breaker, run from a shell on the host.  The
``budget_control`` chat tool does the same things but is operator-only
(``chat_tool_registry.OPERATOR_ONLY_TOOLS``): without a confirmation handler
the chat agent cannot run it, so this is how a latched ``restrict`` or
``shutdown`` is cleared.

State lives in the ``budget_state`` table, so changes made here reach a
running server within its evaluation interval.  Run it as the user the
server runs as, with the same config.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def cmd_budget(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter budget",
        description="Inspect or control the API budget circuit breaker.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show limits, usage and latches")
    sub.add_parser("resume", help="clear latched restrict/shutdown breakers")
    st = sub.add_parser("set-threshold", help="override a named limit's threshold")
    st.add_argument("name")
    st.add_argument("threshold", type=float)
    st.add_argument("--ttl", type=int, default=None,
                    help="seconds until the override reverts")
    sub.add_parser("enable", help="turn the breaker on")
    sub.add_parser("disable", help="turn the breaker off (spend is unbounded)")

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    from . import config
    from .core import budget
    from .db import get_db

    # Never create a database here: run as the wrong user or with the wrong
    # config, sqlite would make an empty one and every command would
    # "succeed" against it while the server's breaker stays untouched.
    db_path = config.CONFIG.get("database_path", "")
    if not db_path or not os.path.isfile(db_path):
        print(
            f"ERROR: no Carpenter database at {db_path or '(database_path unset)'}. "
            "Run this as the server's user with the server's config "
            "(CARPENTER_CONFIG).",
            file=sys.stderr,
        )
        return 1
    try:
        conn = get_db()
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget_state'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(
            f"ERROR: cannot open {db_path}: {e}. Run this as the server's user.",
            file=sys.stderr,
        )
        return 1
    if not has_table:
        print(
            f"ERROR: {db_path} has no budget_state table; start the server "
            "once to initialise it.",
            file=sys.stderr,
        )
        return 1
    print(f"Database: {db_path}", file=sys.stderr)
    if args.command == "status":
        print(json.dumps(budget.status(), indent=2, default=str))
    elif args.command == "resume":
        print("Cleared latches:", json.dumps(budget.resume(), default=str))
    elif args.command == "set-threshold":
        budget.set_threshold_override(args.name, args.threshold, args.ttl)
        suffix = f" (reverts in {args.ttl}s)" if args.ttl else ""
        print(f"Override set: {args.name} threshold -> {args.threshold}{suffix}")
    elif args.command == "enable":
        budget.set_enabled(True)
        print("Budget breaker enabled.")
    elif args.command == "disable":
        budget.set_enabled(False)
        print("Budget breaker DISABLED. Spend is no longer bounded.",
              file=sys.stderr)
    return 0
