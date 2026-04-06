# Model Selection Guide

Carpenter uses a constraint-and-preference model selection system that automatically picks the best model for each task based on quality requirements, cost budget, speed needs, and current model health.

---

## Model Registry

Models are defined in `model_registry.yaml` (synced from `config_seed/model-registry.yaml` on first run):

```yaml
models:
  opus:
    provider: anthropic
    model_id: claude-opus-4-6
    quality_tier: 5              # 1-5 scale
    cost_per_mtok_in: 15.0       # USD per million input tokens
    cost_per_mtok_out: 75.0      # USD per million output tokens
    cached_cost_per_mtok_in: 1.5 # Prompt-cached input rate
    context_window: 200000
    capabilities:
      - planning
      - review
      - implementation
      - code
      - security_review
      - documentation
      - summarization
      - chat
    description: "Top-tier reasoning model"
    measured_speed: null          # Auto-updated by daily reflection (s/ktok output)
```

### Field Reference

| Field | Type | Description |
|---|---|---|
| `provider` | string | `anthropic`, `ollama`, `local`, `tinfoil` |
| `model_id` | string | Provider-specific model identifier |
| `quality_tier` | int | 1 (lowest) to 5 (highest) quality rating |
| `cost_per_mtok_in` | float | Input cost in USD per million tokens |
| `cost_per_mtok_out` | float | Output cost in USD per million tokens |
| `cached_cost_per_mtok_in` | float | Prompt-cached input cost |
| `context_window` | int | Maximum context window in tokens |
| `capabilities` | list | Capability tags for constraint filtering |
| `description` | string | Human-readable description |
| `measured_speed` | float/null | Observed speed (seconds per ktok output), auto-updated |

---

## PolicyConstraints

Hard filters that determine which models are eligible:

```python
@dataclass
class PolicyConstraints:
    min_quality: int = 1                          # Minimum quality tier
    max_quality: int | None = None                # Maximum quality tier
    max_cost_per_mtok_out: float | None = None    # Cost ceiling
    max_latency_s_per_ktok: float | None = None   # Speed ceiling
    required_capabilities: list[str] | None = None # Must-have capabilities
```

Models that fail any constraint are excluded before scoring. Models with unknown `measured_speed` are NOT filtered by latency constraints (benefit of the doubt).

---

## Preference Vector

A 3-tuple `(cost, quality, speed)` that weights the scoring dimensions. Values should sum to ~1.0:

- **Cost weight**: Higher = prefer cheaper models
- **Quality weight**: Higher = prefer higher quality_tier
- **Speed weight**: Higher = prefer faster models

---

## Selection Algorithm

`select_model(policy, current_model, cached_tokens)` follows these steps:

### Step 1: Load Registry
Loads all models from the YAML registry (or config fallback).

### Step 2: Filter by Constraints
Removes models that fail any `PolicyConstraints` check.

### Step 3: Filter by Health
- **3a**: Exclude individual models with `CIRCUIT_OPEN` circuit breakers
- **3b**: Exclude models from providers whose overall health is `CIRCUIT_OPEN`
- **Graceful degradation**: If ALL models would be filtered, keep all eligible (ignore health)

### Step 4: Score
Each dimension is normalized to [0, 1]:

- `cost_score = 1 - (cost / max_cost)` — cheaper = higher
- `quality_score = quality_tier / 5` — higher tier = higher
- `speed_score = 1 - (speed / max_speed)` — faster = higher (unknown speed = median)

Final score: `cost_w * cost_score + quality_w * quality_score + speed_w * speed_score`

### Step 5: Cache-Loss Penalty
When switching providers, cached prompt tokens must be re-sent at full price:

```
switch_cost = cached_tokens * (full_price - cached_price) / 1M
penalty = min(switch_cost * 5.0, 0.5)  # Cap at 0.5 score penalty
```

### Step 6: Return Top Scorer
Returns `SelectionResult(model_key, model_id, score, reason)` or `None`.

---

## Named Presets

Four built-in presets cover common use cases:

### `fast-chat`
```python
constraints: min_quality=2
preference: (0.3, 0.2, 0.5)  # speed-heavy
```
**Use case**: Interactive chat, quick responses. Picks the fastest model with at least tier 2 quality.

### `careful-coding`
```python
constraints: min_quality=4
preference: (0.1, 0.6, 0.3)  # quality-heavy
```
**Use case**: Code generation, implementation tasks. Picks high-quality models (opus or sonnet).

### `background-batch`
```python
constraints: max_cost_per_mtok_out=5.0
preference: (0.6, 0.2, 0.2)  # cost-heavy
```
**Use case**: Bulk processing, summarization, non-critical tasks. Prefers cheap models.

### `caretaker`
```python
constraints: max_quality=2
preference: (0.5, 0.3, 0.2)  # cost-conscious
```
**Use case**: System housekeeping, daily reflections, simple monitoring tasks. Only uses tier 1-2 models.

---

## Custom Policy Examples

### Security Review (High Quality, Cost Cap)

```python
ModelPolicy(
    name="security-review",
    constraints=PolicyConstraints(
        min_quality=5,
        required_capabilities=["security_review"],
    ),
    preference=(0.0, 1.0, 0.0),  # Only quality matters
)
```

### Background Batch with Latency Cap

```python
ModelPolicy(
    name="batch-fast",
    constraints=PolicyConstraints(
        max_cost_per_mtok_out=5.0,
        max_latency_s_per_ktok=2.0,
    ),
    preference=(0.5, 0.1, 0.4),
)
```

### Hard-Pinned Model

```python
ModelPolicy(
    model="anthropic:claude-sonnet-4-5-20250929",  # Bypasses selector entirely
)
```

---

## Adding Custom Models

### Ollama (Local)

Add to `model_registry.yaml`:

```yaml
models:
  my-local-model:
    provider: ollama
    model_id: "llama3.2:8b"
    quality_tier: 2
    cost_per_mtok_in: 0.0
    cost_per_mtok_out: 0.0
    cached_cost_per_mtok_in: 0.0
    context_window: 8192
    capabilities:
      - chat
      - summarization
    description: "Local Llama 3.2 8B via Ollama"
```

Ensure the Ollama endpoint is reachable and the model is pulled.

### Another Cloud Provider

```yaml
models:
  custom-cloud:
    provider: custom
    model_id: "my-custom-model-v1"
    quality_tier: 3
    cost_per_mtok_in: 2.0
    cost_per_mtok_out: 8.0
    cached_cost_per_mtok_in: 0.2
    context_window: 32000
    capabilities:
      - code
      - chat
    description: "Custom cloud model"
```

You'll also need a corresponding client module for the provider or use the chain client configuration.

---

## Speed Auto-Update

The `measured_speed` field is automatically updated by the daily reflection process:

1. Reflection queries `api_calls` table for recent calls with `latency_ms` data
2. Calculates average speed (seconds per ktok output) per model
3. Updates `model_registry.yaml` via `model_registry.update_measured_speed()`
4. Speed data affects model selection scoring on subsequent calls

Models with `measured_speed: null` use the median speed of all measured models during scoring (neither penalized nor favored for speed).

---

## Database Storage

Model policies can be stored in the `model_policies` table for persistent, per-arc policy assignment:

```sql
-- Create a policy
INSERT INTO model_policies (name, policy_json)
VALUES ('my-policy', '{"constraints": {"min_quality": 4}, "preference": [0.1, 0.6, 0.3]}');

-- Assign to an arc
UPDATE arcs SET model_policy_id = ? WHERE id = ?;
```

Use `ModelPolicy.from_db_row()` to deserialize and `policy.to_policy_json()` to serialize.
