# Per-conversation model pin

A single conversation can be pinned to a specific AI provider/model without changing the server's global default. Useful when the user wants to try an alternate backend (e.g. a local Ollama model on their desktop) for just one chat.

## Tools

- **`set_conversation_model(provider, model)`** — pin this conversation. Providers: `anthropic`, `ollama`, `tinfoil`, `chain`, `local`. The model string is the bare identifier (e.g. `qwen3.5:9b`, `claude-haiku-4-5`).
- **`set_conversation_model(clear=true)`** — clear the pin, revert to the global default.
- **`get_conversation_model()`** — show the current pin (or "global default").

## Scope

- Only the current conversation is affected. Other conversations and the server-wide default are untouched — no config file is written.
- The pin is persisted on the `conversations` row (`ai_provider`, `model` columns). It survives server restarts.
- Applies on the next turn: the chat loop reads the pin just before selecting a model.

## When to use

- User says "for this conversation, switch to …" or "just this chat, use …" — pin it.
- User says "go back to the usual model" or "reset this chat" — clear it.
- If the user wants to change the *default* for all chats, that is a server-config change (edit `config.yaml` / `ai_provider`), not this tool.

## Example (Ollama on desktop)

User: "for this conversation only, use the qwen model on my desktop ollama".

Call `set_conversation_model(provider="ollama", model="qwen3.5:9b")`. The Ollama URL itself is a server config (`ollama_url`); this tool only selects the provider + model string.

## Related
[[ai/providers]] · [[ai/config]]
