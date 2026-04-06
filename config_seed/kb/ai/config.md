# AI Configuration

## Core settings
- `ai_provider` — Backend: "anthropic", "ollama", "local", "tinfoil"
- `model_roles` — Map role slots to `provider:model` strings
- `api_standards` — Maps providers to API format (anthropic/openai)

## Model roles
Slots: `default`, `chat`, `default_step`, `title`, `summary`, `compaction`, `code_review`, `review_judge`, and reflection cadences.

Resolution: named slot → `default` slot → auto-detect from `ai_provider`.

## Local inference
- `local_model_path` — Path to GGUF model file
- `local_llama_cpp_path` — Path to llama-server binary
- `local_server_port` — HTTP port (default: 8081)
- `local_context_size` — Context window (default: 8192)
- `local_gpu_layers` — GPU offloading layers (default: 0 = CPU only)
- `local_repack` — Weight repacking: true/false/"auto"

## Related
[[ai/providers]] · [[self-modification/config-tools]]
