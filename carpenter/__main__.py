"""Entry point for running Carpenter as a module."""
import argparse
import os
import sys
from pathlib import Path


def _update_dot_env(dot_env_path: Path, key: str, value: str) -> bool:
    """Write or update KEY=VALUE in a .env file.

    Returns True if the key was updated in place, False if it was newly added.
    """
    import re

    existing_lines: list[str] = []
    if dot_env_path.is_file():
        existing_lines = dot_env_path.read_text().splitlines()

    new_lines: list[str] = []
    updated = False
    for line in existing_lines:
        if re.match(rf'^{re.escape(key)}\s*=', line.strip()):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")  # blank separator
        new_lines.append(f"{key}={value}")

    dot_env_path.parent.mkdir(parents=True, exist_ok=True)
    dot_env_path.write_text("\n".join(new_lines) + "\n")
    return updated


def _enqueue_restart(reason: str = "") -> bool:
    """Insert a platform.restart work item into the DB.

    Safe to call when the server is not running — item will be picked up on next startup
    (or found by the heartbeat if the server is already running).
    Returns True if the item was enqueued, False if an identical item already exists.
    """
    import json
    import sqlite3

    from .config import CONFIG

    db_path = CONFIG.get("database_path", str(Path.home() / "carpenter" / "data" / "platform.db"))
    if not os.path.isfile(db_path):
        return False  # DB not yet initialised; server will pick up .env on next start anyway

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO work_queue "
            "(event_type, payload_json, idempotency_key, max_retries) "
            "VALUES (?, ?, ?, ?)",
            ("platform.restart", json.dumps({"mode": "opportunistic", "reason": reason}),
             "restart-opportunistic", 3),
        )
        enqueued = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return enqueued
    except (sqlite3.Error, OSError) as _exc:
        return False


def _cmd_setup_credential(argv: list[str]) -> None:
    """Handle: python3 -m carpenter setup-credential [options]

    Writes or updates a credential in {base_dir}/.env and enqueues an
    opportunistic restart so the platform picks up the new value when idle.

    Examples:
      # Prompt securely for the value:
      python3 -m carpenter setup-credential --key FORGEJO_TOKEN

      # Supply the value non-interactively:
      python3 -m carpenter setup-credential --key FORGEJO_TOKEN --value ghp_abc123
    """
    import getpass

    from .config import _CREDENTIAL_MAP, CONFIG

    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter setup-credential",
        description="Add or update a credential in your Carpenter installation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
known credential keys:
  {chr(10).join(f'  {k:30s} → config key: {v}' for k, v in sorted(_CREDENTIAL_MAP.items()))}

examples:
  python3 -m carpenter setup-credential --key FORGEJO_TOKEN
  python3 -m carpenter setup-credential --key ANTHROPIC_API_KEY --value sk-ant-...

  Alternatively, export as an environment variable:
    export ANTHROPIC_API_KEY=<value>
  For systemd, add to your service EnvironmentFile or Environment= directive.
""",
    )
    parser.add_argument(
        "--key", required=True,
        help="Credential env-var key (e.g. FORGEJO_TOKEN). Must be a known key.",
    )
    parser.add_argument(
        "--value",
        help="Credential value (if omitted, prompted securely via stdin).",
    )

    args = parser.parse_args(argv)
    key = args.key.strip()

    if key not in _CREDENTIAL_MAP:
        known = ", ".join(sorted(_CREDENTIAL_MAP))
        print(
            f"ERROR: Unknown credential key {key!r}.\n"
            f"Known keys: {known}",
            file=sys.stderr,
        )
        sys.exit(1)

    config_key = _CREDENTIAL_MAP[key]

    # Get value
    value = args.value
    if value is None:
        value = getpass.getpass(f"  Value for {key}: ").strip()
    if not value:
        print("ERROR: Value cannot be empty.", file=sys.stderr)
        sys.exit(1)

    # Write to {base_dir}/.env
    base_dir = CONFIG.get("base_dir", str(Path.home() / "carpenter"))
    dot_env_path = Path(base_dir) / ".env"
    updated = _update_dot_env(dot_env_path, key, value)
    from .platform import get_platform
    get_platform().protect_file(str(dot_env_path))

    action = "Updated" if updated else "Added"
    print(f"  {action} {key} in {dot_env_path}")

    print(f"\nCredential {key!r} installed successfully.")
    print(f"  Config key : {config_key}")
    print(f"  File       : {dot_env_path}")
    print("")
    print("  Alternatively, export as an environment variable:")
    print(f"    export {key}=<value>")
    print("  For systemd, add to your service EnvironmentFile or Environment= directive.")

    # Enqueue opportunistic restart
    enqueued = _enqueue_restart(reason=f"credential added: {key}")
    if enqueued:
        print("")
        print("  Opportunistic restart queued — platform will restart when idle.")
    else:
        print("")
        print("  Restart Carpenter to apply the new credential.")
    print("")


def main():
    # Dispatch subcommands before full argparse (preserves backward compat)
    if len(sys.argv) > 1 and sys.argv[1] == "setup-credential":
        _cmd_setup_credential(sys.argv[2:])
        return

    from .server import run_server
    run_server()


if __name__ == "__main__":
    main()
