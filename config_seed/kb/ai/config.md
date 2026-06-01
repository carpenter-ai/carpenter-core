# AI Configuration

## Core settings
- `ai_provider` — Backend: "anthropic", "ollama", "tinfoil", "chain"
- `model_roles` — Map role slots to `provider:model` strings
- `api_standards` — Maps providers to API format (anthropic/openai)

## Model roles
Slots: `default`, `chat`, `default_step`, `title`, `summary`, `compaction`, `code_review`, `review_judge`, and reflection cadences.

Resolution: named slot → `default` slot → auto-detect from `ai_provider`.

## Related
[[ai/providers]] · [[self-modification/config-tools]]
