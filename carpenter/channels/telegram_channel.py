"""Telegram channel connector — Bot API via long-polling or webhooks.

Uses httpx (already a dependency) to communicate with the Telegram Bot API.
No additional packages required.

Polling mode (default) works out of the box — no public IP or TLS needed.
Webhook mode requires a publicly reachable domain with TLS (Telegram only
sends webhooks over HTTPS). It is not part of the standard install flow;
users who want it can set ``mode: webhook`` and ``webhook_path`` in
config.yaml after setting up TLS.
"""

import asyncio
import logging
import os
from datetime import datetime

import httpx

from .base import HealthStatus
from .channel import ChannelConnector
from .formatting import format_for_channel, split_message

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_LENGTH = 4096


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
        # Webhook router (set during start if mode=webhook)
        self.routes = None

    async def start(self, config: dict) -> None:
        """Start the Telegram connector.

        Validates the bot token, then starts polling or webhook mode.
        """
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
                logger.debug("Failed to delete webhook on stop")

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
                # Retry without parse_mode (formatting may be invalid)
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
                    pass
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

    async def _setup_webhook(self, config: dict) -> None:
        """Register webhook with Telegram and create Starlette routes."""
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        # Build webhook URL from platform config
        host = config.get("tls_domain", "localhost")
        port = config.get("port", 7842)
        scheme = "https" if config.get("tls_enabled") else "http"
        webhook_url = f"{scheme}://{host}:{port}{self._webhook_path}"

        # Register webhook with Telegram
        await self._api("setWebhook", url=webhook_url)

        # Create route
        connector = self  # closure reference

        async def telegram_webhook(request: Request):
            body = await request.json()
            try:
                await connector._handle_update(body)
            except Exception:
                logger.exception("Error handling Telegram webhook")
            return Response(status_code=200)

        self.routes = [
            Route(self._webhook_path, telegram_webhook, methods=["POST"]),
        ]
