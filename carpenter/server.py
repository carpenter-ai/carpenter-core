"""Reusable server startup for Carpenter.

Platform packages (e.g. carpenter-linux) call run_server() after injecting
their platform, sandbox, and executor implementations.
"""

import argparse
import logging
import os
import sys


_SAFE_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _check_bind_safety(host: str, cfg: dict) -> str | None:
    """Return an error message if binding to *host* is unsafe, else None.

    Unsafe = non-loopback host with no ui_token, no TLS, and
    allow_insecure_bind not set.
    """
    if host in _SAFE_HOSTS:
        return None
    if cfg.get("ui_token"):
        return None
    if cfg.get("tls_enabled"):
        logging.getLogger(__name__).warning(
            "TLS is enabled but no ui_token is set. "
            "While the connection is encrypted, authentication is recommended."
        )
        return None
    if cfg.get("allow_insecure_bind"):
        return None
    return (
        f"Refusing to bind to {host} without authentication.\n"
        "Set a ui_token in config.yaml, or pass "
        "--host 127.0.0.1 for local-only access.\n"
        "To override this check, set allow_insecure_bind: true in config.yaml."
    )


def _check_tls_config(cfg: dict) -> str | None:
    """Return an error message if TLS config is invalid, else None.

    Validates:
    - If tls_enabled, cert path, key path, and domain must be provided
    - Certificate and key files must exist and be readable
    - CA file (if set) must exist and be readable
    """
    if not cfg.get("tls_enabled"):
        return None

    cert_path = cfg.get("tls_cert_path", "").strip()
    key_path = cfg.get("tls_key_path", "").strip()
    domain = cfg.get("tls_domain", "").strip()

    if not cert_path:
        return "tls_enabled is True but tls_cert_path is not set"
    if not key_path:
        return "tls_enabled is True but tls_key_path is not set"
    if not domain:
        return "tls_enabled is True but tls_domain is not set"

    # Paths already expanded by _expand_paths() during config load

    if not os.path.isfile(cert_path):
        return f"TLS certificate file not found: {cert_path}"
    if not os.path.isfile(key_path):
        return f"TLS private key file not found: {key_path}"

    for label, path in [("certificate", cert_path), ("private key", key_path)]:
        try:
            with open(path, "r") as f:
                f.read(1)
        except (OSError, PermissionError) as e:
            return f"Cannot read TLS {label} file {path}: {e}"

    ca_path = cfg.get("tls_ca_path", "").strip()
    if ca_path:
        if not os.path.isfile(ca_path):
            return f"TLS CA file not found: {ca_path}"
        try:
            with open(ca_path, "r") as f:
                f.read(1)
        except (OSError, PermissionError) as e:
            return f"Cannot read TLS CA file {ca_path}: {e}"

    return None


def run_server(argv=None):
    """Parse arguments and start the Carpenter HTTP server.

    Args:
        argv: Command-line arguments. Defaults to sys.argv[1:].
    """
    parser = argparse.ArgumentParser(description="Carpenter AI Agent Platform")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    logger = logging.getLogger(__name__)

    # Health check: verify cryptography library for encryption
    try:
        from cryptography.fernet import Fernet
        Fernet.generate_key()
        logger.info("Encryption: cryptography library available")
    except ImportError:
        logger.warning(
            "cryptography library not available - untrusted arc output will NOT be encrypted. "
            "Install with: pip install cryptography>=41.0"
        )
    except (OSError, ValueError, RuntimeError) as e:
        logger.warning(
            "cryptography library present but not functional: %s. "
            "Encryption may fail at runtime.",
            e
        )

    from .config import CONFIG

    host = args.host if args.host is not None else CONFIG.get("host", "127.0.0.1")
    port = args.port if args.port is not None else CONFIG.get("port", 7842)

    # Safety check: refuse non-loopback bind without auth
    err = _check_bind_safety(host, CONFIG)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    # Validate TLS configuration
    tls_err = _check_tls_config(CONFIG)
    if tls_err:
        print(f"ERROR: {tls_err}", file=sys.stderr)
        sys.exit(1)

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is required. Install it with: pip install uvicorn")
        sys.exit(1)

    from .api.http import create_app
    app = create_app()

    uvicorn_kwargs = {}
    if CONFIG.get("tls_enabled"):
        uvicorn_kwargs["ssl_keyfile"] = CONFIG["tls_key_path"]
        uvicorn_kwargs["ssl_certfile"] = CONFIG["tls_cert_path"]
        key_password = (os.environ.get("TLS_KEY_PASSWORD") or CONFIG.get("tls_key_password", "")).strip()
        if key_password:
            uvicorn_kwargs["ssl_keyfile_password"] = key_password
        logger.info(
            "TLS enabled — serving HTTPS on %s:%s (domain: %s)",
            host, port, CONFIG["tls_domain"],
        )
    else:
        logger.info("Serving HTTP on %s:%s", host, port)

    uvicorn.run(app, host=host, port=port, **uvicorn_kwargs)
