"""Signal channel connector — signal-cli subprocess or REST API.

Two modes:
- subprocess (default): Runs signal-cli as a JSON-RPC subprocess on stdio.
  No additional Python packages required — signal-cli is a separate Java binary.
- rest_api: Connects to signal-cli-rest-api (Docker container) over HTTP.
  Receives inbound messages via webhook; sends outbound via POST.
  Uses httpx (already a dependency).
"""

import asyncio
import json
import logging
import os
import shutil
import signal
from datetime import datetime

from .base import HealthStatus
from .channel import ChannelConnector
from .formatting import split_message

logger = logging.getLogger(__name__)

SIGNAL_MAX_LENGTH = 6000


class SignalChannelConnector(ChannelConnector):
    """Channel connector for Signal via signal-cli.

    Supports two modes:
    - subprocess: Runs signal-cli as a JSON-RPC subprocess (default)
    - rest_api: Connects to signal-cli-rest-api over HTTP + webhook
    """

    channel_type = "signal"

    def __init__(self, name: str = "signal", connector_config: dict | None = None):
        self.name = name
        cc = connector_config or {}
        self.enabled = cc.get("enabled", False)
        self._account = cc.get("account", "")
        self._allowed_numbers: list[str] = [
            str(n) for n in cc.get("allowed_numbers", [])
        ]
        self._last_healthy: datetime | None = None

        # Mode: "subprocess" (default) or "rest_api"
        self._mode = cc.get("mode", "subprocess")

        # Subprocess mode fields
        self._cli_path = cc.get(
            "signal_cli_path",
            shutil.which("signal-cli") or "signal-cli",
        )
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._rpc_id = 0
        self._write_lock = asyncio.Lock()

        # REST API mode fields
        self._rest_api_url = cc.get("rest_api_url", "")
        self._webhook_path = cc.get("webhook_path", "/hooks/signal")
        self._client = None  # httpx.AsyncClient
        self.routes = None   # Starlette Route list (set during start if rest_api)

    # -- Lifecycle --

    async def start(self, config: dict) -> None:
        """Start the Signal connector in the configured mode."""
        if not self._account:
            raise ValueError("Signal account phone number is required")

        if self._mode == "rest_api":
            await self._start_rest_api(config)
        else:
            await self._start_subprocess()

    async def _start_subprocess(self) -> None:
        """Start signal-cli as a JSON-RPC subprocess."""
        if not os.path.isfile(self._cli_path):
            raise FileNotFoundError(f"signal-cli not found at {self._cli_path}")
        if not os.access(self._cli_path, os.X_OK):
            raise PermissionError(f"signal-cli at {self._cli_path} is not executable")

        self._process = await asyncio.create_subprocess_exec(
            self._cli_path, "-a", self._account, "jsonRpc",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._reader_task = asyncio.create_task(self._read_loop())
        self._last_healthy = datetime.now()
        logger.info("Signal connector started for account %s (pid=%d)",
                     self._account, self._process.pid)

    async def _start_rest_api(self, config: dict) -> None:
        """Connect to signal-cli-rest-api over HTTP and set up webhook."""
        if not self._rest_api_url:
            raise ValueError("rest_api_url is required for rest_api mode")

        import httpx
        self._client = httpx.AsyncClient(timeout=30)

        # Validate connectivity
        try:
            resp = await self._client.get(f"{self._rest_api_url}/v1/health")
            resp.raise_for_status()
        except Exception as e:
            await self._client.aclose()
            self._client = None
            raise ConnectionError(
                f"Cannot reach signal-cli-rest-api at {self._rest_api_url}: {e}"
            ) from e

        self._setup_webhook()
        self._last_healthy = datetime.now()
        logger.info("Signal REST API connector started for account %s at %s",
                     self._account, self._rest_api_url)

    async def stop(self) -> None:
        """Stop the connector."""
        if self._mode == "rest_api":
            await self._stop_rest_api()
        else:
            await self._stop_subprocess()

    async def _stop_subprocess(self) -> None:
        """Stop signal-cli subprocess gracefully."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._process and self._process.returncode is None:
            try:
                self._process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning("signal-cli did not stop after SIGTERM, sending SIGKILL")
                    self._process.kill()
                    await self._process.wait()
            except ProcessLookupError:
                pass  # Already exited
            self._process = None

    async def _stop_rest_api(self) -> None:
        """Close the httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- Health --

    async def health_check(self) -> HealthStatus:
        """Check connector health."""
        if self._mode == "rest_api":
            return await self._health_check_rest_api()
        return await self._health_check_subprocess()

    async def _health_check_subprocess(self) -> HealthStatus:
        """Check if signal-cli subprocess is alive."""
        if not self._process or self._process.returncode is not None:
            return HealthStatus(
                healthy=False,
                detail="subprocess not running",
                last_seen=self._last_healthy,
            )
        return HealthStatus(
            healthy=True,
            detail=f"pid={self._process.pid}",
            last_seen=self._last_healthy,
        )

    async def _health_check_rest_api(self) -> HealthStatus:
        """Check signal-cli-rest-api connectivity."""
        if not self._client:
            return HealthStatus(
                healthy=False,
                detail="not started",
                last_seen=self._last_healthy,
            )
        try:
            resp = await self._client.get(f"{self._rest_api_url}/v1/health")
            resp.raise_for_status()
            self._last_healthy = datetime.now()
            return HealthStatus(
                healthy=True,
                detail=f"rest_api {self._rest_api_url}",
                last_seen=self._last_healthy,
            )
        except Exception as e:
            return HealthStatus(
                healthy=False,
                detail=str(e),
                last_seen=self._last_healthy,
            )

    # -- Send --

    async def send_message(self, conversation_id: int, text: str,
                           metadata: dict | None = None) -> bool:
        """Send a message via the configured mode."""
        if self._mode == "rest_api":
            return await self._send_message_rest_api(conversation_id, text)
        return await self._send_message_subprocess(conversation_id, text)

    async def _send_message_subprocess(self, conversation_id: int, text: str) -> bool:
        """Send via signal-cli JSON-RPC stdin."""
        if not self._process or self._process.returncode is not None:
            return False

        recipient = self._resolve_recipient(conversation_id)
        if recipient is None:
            logger.warning("No Signal recipient for conversation %s", conversation_id)
            return False

        chunks = split_message(text, SIGNAL_MAX_LENGTH)
        for chunk in chunks:
            try:
                await self._rpc_send("send", {
                    "recipient": [recipient],
                    "message": chunk,
                })
            except Exception:
                logger.exception("Failed to send Signal message to %s", recipient)
                return False

            if len(chunks) > 1:
                await asyncio.sleep(0.3)

        return True

    async def _send_message_rest_api(self, conversation_id: int, text: str) -> bool:
        """Send via signal-cli-rest-api HTTP endpoint."""
        if not self._client:
            return False

        recipient = self._resolve_recipient(conversation_id)
        if recipient is None:
            logger.warning("No Signal recipient for conversation %s", conversation_id)
            return False

        chunks = split_message(text, SIGNAL_MAX_LENGTH)
        for chunk in chunks:
            try:
                resp = await self._client.post(
                    f"{self._rest_api_url}/v2/send",
                    json={
                        "message": chunk,
                        "number": self._account,
                        "recipients": [recipient],
                    },
                )
                resp.raise_for_status()
            except Exception:
                logger.exception("Failed to send Signal REST API message to %s", recipient)
                return False

            if len(chunks) > 1:
                await asyncio.sleep(0.3)

        return True

    # -- Internal: subprocess mode --

    async def _read_loop(self) -> None:
        """Read newline-delimited JSON from signal-cli stdout."""
        backoff = 1
        while True:
            try:
                line = await self._process.stdout.readline()
                if not line:
                    # EOF — process exited
                    logger.warning("signal-cli stdout closed (process exited)")
                    break

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from signal-cli: %s", line[:200])
                    continue

                self._last_healthy = datetime.now()
                backoff = 1

                # Handle incoming messages
                method = data.get("method")
                if method == "receive":
                    await self._handle_receive(data.get("params", {}))

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in Signal read loop")
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

    async def _rpc_send(self, method: str, params: dict) -> None:
        """Send a JSON-RPC request to signal-cli via stdin."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("signal-cli process not running")

        self._rpc_id += 1
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._rpc_id,
        }

        line = json.dumps(request) + "\n"
        async with self._write_lock:
            self._process.stdin.write(line.encode())  # sync method
            await self._process.stdin.drain()  # async flush

    # -- Internal: REST API mode --

    def _setup_webhook(self) -> None:
        """Create Starlette routes for the webhook endpoint."""
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        connector = self  # closure reference

        async def signal_webhook(request: Request):
            body = await request.json()
            try:
                # signal-cli-rest-api forwards JSON-RPC messages:
                #   {"method": "receive", "params": {"envelope": {...}}}
                # Also handle raw envelope payloads (resilience):
                #   {"envelope": {...}}
                if "params" in body:
                    params = body["params"]
                elif "envelope" in body:
                    params = body
                else:
                    return Response(status_code=200)

                await connector._handle_receive(params)
            except Exception:
                logger.exception("Error handling Signal webhook")
            return Response(status_code=200)

        self.routes = [
            Route(self._webhook_path, signal_webhook, methods=["POST"]),
        ]

    async def _rest_api_reject(self, recipient: str, message: str) -> None:
        """Send a rejection message via REST API (best-effort)."""
        if not self._client:
            return
        try:
            await self._client.post(
                f"{self._rest_api_url}/v2/send",
                json={
                    "message": message,
                    "number": self._account,
                    "recipients": [recipient],
                },
            )
        except Exception:
            pass

    # -- Internal: shared --

    async def _handle_receive(self, params: dict) -> None:
        """Process a received message from signal-cli."""
        envelope = params.get("envelope", {})
        source = envelope.get("source") or envelope.get("sourceNumber", "")
        if not source:
            return

        # Ignore messages from our own account (sent-message receipts / sync)
        if self._account and source == self._account:
            return

        data_msg = envelope.get("dataMessage", {})
        text = data_msg.get("message")
        if not text:
            return

        # Allowlist check
        if not self._check_allowed(source):
            if self._mode == "rest_api":
                await self._rest_api_reject(
                    source,
                    "Sorry, you are not authorized to use this service.",
                )
            else:
                try:
                    await self._rpc_send("send", {
                        "recipient": [source],
                        "message": "Sorry, you are not authorized to use this service.",
                    })
                except Exception:
                    pass
            return

        # Extract display name from profile if available
        display_name = envelope.get("sourceName", "")

        await self.deliver_inbound(
            channel_user_id=source,
            text=text,
            display_name=display_name or None,
        )

    def _check_allowed(self, phone_number: str) -> bool:
        """Check if a phone number is in the allowlist.

        Empty allowlist = allow all.
        """
        if not self._allowed_numbers:
            return True
        return phone_number in self._allowed_numbers

    def _resolve_recipient(self, conversation_id: int) -> str | None:
        """Look up the Signal phone number for a conversation."""
        from ..db import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT channel_user_id FROM channel_bindings "
                "WHERE channel_type = 'signal' AND conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            return row["channel_user_id"] if row else None
        finally:
            conn.close()
