# AI Providers

Carpenter supports multiple AI backends.

## Available providers
- **anthropic** — Claude models via Anthropic API (native format)
- **ollama** — Models served by an Ollama server over HTTP (OpenAI-compatible format)
- **tinfoil** — Tinfoil encrypted inference (OpenAI-compatible)
- **chain** — Virtual provider that chains multiple backends (see inference_chain config)

## API standard normalization
Each provider maps to an API standard: `"anthropic"` or `"openai"`. The normalization layer translates tool definitions and responses automatically.

## Adding a new provider
1. Set its standard in `api_standards` config
2. Implement `call()` with `tools` parameter
3. Register in `_get_client()` / `_get_provider_for_client()`

## Related
[[ai/config]] · [[ai/tools]]
