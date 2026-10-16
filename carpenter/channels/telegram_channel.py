"""Telegram channel connector — Bot API via long-polling or webhooks.

Uses httpx (already a dependency) to communicate with the Telegram Bot API.
No additional packages required.

Polling mode (default) works out of the box — no public IP or TLS needed.
Webhook mode requires a publicly reachable domain with TLS (Telegram only
sends webhooks over HTTPS). It is not part of the standard install flow;
users who want it can set ``mode: webhook`` and ``webhook_path`` in
config.yaml after setting up TLS.

Webhook authentication
----------------------
The webhook path sits under ``/hooks/``, which the UI-token middleware does
not gate, and a Telegram update's sender id is whatever the caller wrote.
When the ``telegram_webhook_secret`` config key is set (credential env var
``TELEGRAM_WEBHOOK_SECRET``), the connector registers it with Telegram as
setWebhook's ``secret_token``, and every webhook request must carry it in
the ``X-Telegram-Bot-Api-Secret-Token`` header; anything else gets a 401
and is never processed.  When the secret is unset the webhook stays open,
for backward compatibility, and a warning is logged at start.  Polling mode
has no webhook and is unaffected.
"""

import asyncio
import hmac
import logging
import os
import re
from datetime import datetime

import httpx

from .. import constants
from .base import HealthStatus
from .channel import ChannelConnector
from .formatting import format_for_channel, split_message

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_LENGTH = 4096

# Header in which Telegram echoes setWebhook's secret_token.
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
# Telegram accepts 1-256 characters from this set for secret_token.
_SECRET_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")


class TelegramChannelConnector(ChannelConnector):
    """Channel connector for Telegram Bot API.

    Supports two modes:
    - polling: Long-polls getUpdates (default, no public IP needed)
    - webhook: Registers a FastAPI route + calls setWebhook
    """

    channel_type = "telegram"

    def __init__(self, name: str = "telegram", connector_config: dict | None = None):
        self.name = name
        cc = connector_config or {}
        self.enabled = cc.get("enabled", False)
        self._bot_token = cc.get("bot_token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._mode = cc.get("mode", "polling")
        self._webhook_path = cc.get("webhook_path", "/hooks/telegram")
        self._allowed_users: list[str] = [
            str(u) for u in cc.get("allowed_users", [])
        ]
        self._parse_mode = cc.get("parse_mode", "MarkdownV2")
        self._poll_task: asyncio.Task | None = None
        self._client = None
        self._bot_username: str | None = None
        self._last_healthy: datetime | None = None
        self._consecutive_errors = 0
        # Webhook shared secret; a credential, so read from platform config
        # at start, never from the connector's YAML block.
        self._webhook_secret = ""
        # Webhook router (set during start if mode=webhook)
        self.routes = None

    async def start(self, config: dict) -> None:
        """Start the Telegram connector.

        Validates the bot token, then starts polling or webhook mode.
        """
        if not self._bot_token:
            # TELEGRAM_BOT_TOKEN set in {base_dir}/.env reaches config, not
            # os.environ, so look there too.
            self._bot_token = str(config.get("telegram_bot_token") or "")
        if not self._bot_token:
            raise ValueError("Telegram bot_token is required")

        self._client = httpx.AsyncClient(timeout=60)

        # Validate bot token via getMe
        me = await self._api("getMe")
        self._bot_username = me.get("username", "unknown")
        logger.info("Telegram bot connected: @%s", self._bot_username)

        if self._mode == "polling":
            # Delete any stale webhook before polling
            await self._api("deleteWebhook")
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("Telegram polling started")
        elif self._mode == "webhook":
            await self._setup_webhook(config)
            logger.info("Telegram webhook registered at %s", self._webhook_path)

        self._last_healthy = datetime.now()

    async def stop(self) -> None:
        """Stop the connector and clean up."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._mode == "webhook" and self._client:
            try:
                await self._api("deleteWebhook")
            except Exception:
                # Best-effort cleanup; do not bubble during shutdown.
                logger.info("Failed to delete Telegram webhook on stop", exc_info=True)

        if self._client:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> HealthStatus:
        """Check bot connectivity."""
        if not self._client:
            return HealthStatus(healthy=False, detail="not started")

        if self._consecutive_errors > 10:
            return HealthStatus(
                healthy=False,
                detail=f"{self._consecutive_errors} consecutive errors",
                last_seen=self._last_healthy,
            )

        try:
            await self._api("getMe")
            self._last_healthy = datetime.now()
            return HealthStatus(
                healthy=True,
                detail=f"@{self._bot_username}",
                last_seen=self._last_healthy,
            )
        except Exception as e:
            logger.exception("Telegram health check failed for @%s", self._bot_username)
            return HealthStatus(
                healthy=False,
                detail=str(e),
                last_seen=self._last_healthy,
            )

    async def send_message(self, conversation_id: int, text: str,
                           metadata: dict | None = None) -> bool:
        """Send a message to a Telegram chat."""
        if not self._client:
            return False

        # Resolve the Telegram chat_id from the conversation
        chat_id = self._resolve_chat_id(conversation_id)
        if chat_id is None:
            logger.warning("No Telegram chat_id for conversation %s", conversation_id)
            return False

        # Split long messages
        chunks = split_message(text, TELEGRAM_MAX_LENGTH)
        for chunk in chunks:
            try:
                await self._api("sendMessage",
                                chat_id=int(chat_id),
                                text=chunk,
                                parse_mode=self._parse_mode)
            except Exception:
                # Retry without parse_mode (formatting may be invalid).
                # Intentional suppression: if the retry succeeds, log the
                # original failure for diagnostics; otherwise the inner
                # except logs the retry failure with traceback.
                logger.info(
                    "Telegram sendMessage with parse_mode=%s failed for chat_id=%s; retrying without parse_mode",
                    self._parse_mode, chat_id, exc_info=True,
                )
                try:
                    await self._api("sendMessage",
                                    chat_id=int(chat_id),
                                    text=chunk)
                except Exception:
                    logger.exception("Failed to send Telegram message to %s", chat_id)
                    return False

            # Small delay between chunks to preserve ordering
            if len(chunks) > 1:
                await asyncio.sleep(0.3)

        return True

    # -- Internal methods --

    async def _api(self, method: str, **params) -> dict:
        """Call the Telegram Bot API."""
        url = f"{TELEGRAM_API}/bot{self._bot_token}/{method}"
        response = await self._client.post(url, json=params if params else None)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
        return data.get("result", {})

    async def _poll_loop(self) -> None:
        """Long-poll for updates from Telegram."""
        offset = 0
        backoff = 1

        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    offset=offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
                self._consecutive_errors = 0
                backoff = 1

                for update in updates:
                    offset = update["update_id"] + 1
                    await self._handle_update(update)

            except asyncio.CancelledError:
                raise
            except Exception:
                self._consecutive_errors += 1
                logger.exception(
                    "Telegram poll error (%d consecutive)",
                    self._consecutive_errors,
                )
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)

    async def _handle_update(self, update: dict) -> None:
        """Process a single Telegram update."""
        message = update.get("message")
        if not message:
            return

        text = message.get("text")
        if not text:
            return

        sender = message.get("from", {})
        user_id = str(sender.get("id", ""))
        username = sender.get("username", "")
        display_name = sender.get("first_name", "")

        # Allowlist check
        if not self._check_allowed(user_id, username):
            chat_id = message.get("chat", {}).get("id")
            if chat_id:
                try:
                    await self._api(
                        "sendMessage",
                        chat_id=chat_id,
                        text="Sorry, you are not authorized to use this bot.",
                    )
                except Exception:
                    # Best-effort rejection notice; do not bubble.
                    logger.info("Telegram rejection send failed for chat_id=%s", chat_id, exc_info=True)
            return

        await self.deliver_inbound(
            channel_user_id=user_id,
            text=text,
            display_name=display_name,
        )

    def _check_allowed(self, user_id: str, username: str) -> bool:
        """Check if a user is in the allowlist.

        Empty allowlist = allow all.
        """
        if not self._allowed_users:
            return True
        return user_id in self._allowed_users or username in self._allowed_users

    def _resolve_chat_id(self, conversation_id: int) -> str | None:
        """Look up the Telegram chat_id for a conversation via channel_bindings."""
        from ..db import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT channel_user_id FROM channel_bindings "
                "WHERE channel_type = 'telegram' AND conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            return row["channel_user_id"] if row else None
        finally:
            conn.close()

    def _webhook_authorized(self, request) -> bool:
        """Return True if *request* may be processed by the webhook.

        With no secret configured every request is allowed (legacy
        behaviour, warned about at start).  Otherwise the request must
        carry the exact secret in ``X-Telegram-Bot-Api-Secret-Token``.
        Comparison is constant-time; any error denies.
        """
        expected = self._webhook_secret
        if not expected:
            return True
        try:
            provided = request.headers.get(WEBHOOK_SECRET_HEADER) or ""
            if not provided:
                return False
            return hmac.compare_digest(
                provided.encode("utf-8"), expected.encode("utf-8"),
            )
        except Exception:
            # Fail closed: an unparseable request is not an authorized one.
            # The traceback carries no header values.
            logger.warning(
                "Telegram webhook secret check failed; denying request",
                exc_info=True,
            )
            return False

    async def _setup_webhook(self, config: dict) -> None:
        """Register webhook with Telegram and create Starlette routes."""
        secret = str(config.get("telegram_webhook_secret") or "")
        if secret and not _SECRET_TOKEN_RE.fullmatch(secret):
            # Telegram would reject it in setWebhook anyway; say why without
            # echoing the value.
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET must be 1-256 characters of "
                "A-Z, a-z, 0-9, '_' or '-'"
            )
        self._webhook_secret = secret

        # Build webhook URL from platform config
        host = config.get("tls_domain", "localhost")
        port = config.get("port", constants.DEFAULT_API_PORT)
        scheme = "https" if config.get("tls_enabled") else "http"
        webhook_url = f"{scheme}://{host}:{port}{self._webhook_path}"

        # Register webhook with Telegram
        if secret:
            await self._api("setWebhook", url=webhook_url, secret_token=secret)
        else:
            logger.warning(
                "Telegram webhook %s is UNAUTHENTICATED: anyone who can reach "
                "this server can post an update that appears to come from an "
                "allowed user. Set TELEGRAM_WEBHOOK_SECRET to have it "
                "registered with Telegram and checked on every request.",
                self._webhook_path,
            )
            await self._api("setWebhook", url=webhook_url)

        self._build_webhook_routes()

    def _build_webhook_routes(self) -> None:
        """Create the Starlette route for the webhook endpoint."""
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        connector = self  # closure reference

        async def telegram_webhook(request: Request):
            # Authenticate before reading the body.  Never log the secret.
            if not connector._webhook_authorized(request):
                logger.warning(
                    "Rejected Telegram webhook request from %s: missing or "
                    "invalid secret token",
                    request.client.host if request.client else "unknown",
                )
                return Response(status_code=401)

            body = await request.json()
            try:
                await connector._handle_update(body)
            except Exception:
                logger.exception("Error handling Telegram webhook")
            return Response(status_code=200)

        self.routes = [
            Route(self._webhook_path, telegram_webhook, methods=["POST"]),
        ]
