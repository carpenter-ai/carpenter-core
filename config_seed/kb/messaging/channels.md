# Channel Connectors

Carpenter supports multiple messaging channels.

## Available channels
- **Web** — Built-in web UI at the configured host:port
- **Signal** — Signal messenger (subprocess or REST API mode)
- **Telegram** — Telegram bot (polling or webhook mode)

## Configuration
Channels are defined in the `connectors` config key. Each connector has a `type` and channel-specific settings.

## Identity resolution
Each channel maps external user IDs to conversations via `channel_bindings`. A new user gets a new conversation automatically.

## Related
[[messaging/tools]] · [[self-modification/config-tools]]
