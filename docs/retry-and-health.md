# Retry and Health System

Carpenter uses a multi-layered retry and health system to handle transient API failures gracefully while preventing resource waste on persistent outages.

---

## Overview

There are two levels of retry:

1. **Mechanical retries** (`_call_with_retries` in `invocation.py`): Low-level HTTP-level retries with exponential backoff, scoped to a single API call attempt.
2. **Arc-level retries** (`arc_retry.py`): Higher-level retries that re-dispatch entire arcs after classifying the error, with per-error-type budgets and escalation policies.

On top of retries, the **model health** system tracks per-model success rates and implements circuit breakers to temporarily blacklist failing models. The **health monitor** runs alongside the work queue scanner (every ~5s) to detect and notify on notable health events.

---

## Error Types

Errors are classified by `error_classifier.py` into semantic categories:

| Error Type | Retriable | Max Retries | Backoff Strategy | Escalate on Exhaust |
|---|---|---|---|---|
| `RateLimitError` | Yes | 5 | `max(10, retry_after)` + jitter | No |
| `APIOutageError` | Yes | 4 | Exponential, cap 300s (5 min) | Yes |
| `NetworkError` | Yes | 3 | Exponential, cap 60s (1 min) | No |
| `VerificationError` | Yes | 2 | Immediate (0s backoff) | No |
| `UnknownError` | Yes | 2 | Fixed 5s + jitter | No |
| `AuthError` | No | 0 | N/A | No |
| `ModelError` | No | 0 | N/A | Yes |
| `ClientError` | No | 0 | N/A | No |

Backoff uses exponential base 2 (`2^attempt`) with ±10% jitter, multiplied by the model health backoff multiplier (1x–4x).

---

## Model Health States

Per-model health is tracked via a sliding window of the last 20 API call outcomes:

| State | Success Rate | Consecutive Failures | Backoff Multiplier | Behavior |
|---|---|---|---|---|
| `HEALTHY` | ≥ 80% | < 5 | 1.0x | Normal operation |
| `DEGRADED` | 50%–80% | < 5 | 2.0x | Increased backoff, still usable |
| `UNHEALTHY` | < 50% | < 5 | 4.0x | Heavy backoff, consider escalation |
| `CIRCUIT_OPEN` | Any | ≥ 5 | 4.0x | Refuse requests, auto-recover after 60s |

### Circuit Breaker Behavior

- **Opens** after 5 consecutive failures for a model
- **Half-open** after 60 seconds — next request is attempted as a probe
- If probe succeeds, health is recalculated from the sliding window
- If probe fails, circuit re-opens for another 60 seconds
- Manual reset: `model_health.reset_circuit_breaker(model_id)`

---

## Local Fallback

When all cloud retries are exhausted, the system can fall back to a local model (e.g., Ollama running on the network).

### How It Works

1. After `_call_with_retries()` exhausts all retry attempts (or all cloud models are circuit-open), it calls `_try_local_fallback()`
2. Fallback is only attempted when `tools is None` (tool calling not supported in fallback)
3. The function checks config-level operation filtering (allowed/blocked lists)
4. Messages are converted from Anthropic to OpenAI format and truncated to fit the local model's context window
5. A direct `httpx.post` call is made to the configured endpoint (bypassing client-level circuit breakers)
6. Success/failure is recorded in model health under the `fallback:` prefix

### Fast Detection ("All Cloud Down")

Before entering the retry loop, `_call_with_retries()` checks `all_cloud_models_circuit_open()`. If all cloud models (cost > 0 in the registry) have open circuit breakers AND no tools are needed, it skips retries entirely and goes straight to local fallback. This avoids unnecessary retry delays when cloud is known to be down.

### Per-Arc Override

Individual arcs can control fallback behavior via `arc_state`:

```python
# Block fallback for this arc (e.g., security-sensitive operations)
_set_arc_state(db, arc_id, "_fallback_allowed", False)

# Explicitly allow (or leave absent for default behavior)
_set_arc_state(db, arc_id, "_fallback_allowed", True)
```

When `_fallback_allowed` is `False`, the fallback is skipped regardless of config.

### Operation Filtering

The `local_fallback` config controls which operations can use fallback:

- `allowed_operations`: Whitelist (if non-empty, only these are allowed)
- `blocked_operations`: Blacklist (always checked first)

Default: allow `chat`, `summarization`, `simple_code`; block `review`, `security_review`, `planning`.

---

## Provider-Level Health

Health is also aggregated at the provider level:

- **Provider CIRCUIT_OPEN**: All models for a provider are `CIRCUIT_OPEN`
- **Provider health**: Otherwise, the worst non-circuit state across models (UNHEALTHY > DEGRADED > HEALTHY)

Provider health is used in model selection (step 3b): models from a provider whose overall health is `CIRCUIT_OPEN` are filtered out, with graceful degradation if all providers are down.

### Provider Outage Detection

The health monitor detects when all models for a provider are `CIRCUIT_OPEN` and sends an urgent notification (category: `provider_outage`), with dedup to avoid spam. Recovery clears the dedup state.

---

## Configuration Reference

### `arc_retry` Section

```yaml
arc_retry:
  enabled: true                      # Master switch for arc-level retries
  default_policy: "transient_only"   # transient_only | aggressive | conservative
  max_retries:
    RateLimitError: 5
    APIOutageError: 4
    NetworkError: 3
    UnknownError: 2
    VerificationError: 2
    default: 3
  backoff_caps:
    RateLimitError: 600              # 10 minutes
    APIOutageError: 300              # 5 minutes
    NetworkError: 60                 # 1 minute
    VerificationError: 0             # immediate
    default: 120                     # 2 minutes
  backoff_base: 2                    # Exponential base (2^attempt)
  jitter_percent: 10                 # ±10% randomization
  escalate_on_exhaust:
    RateLimitError: false
    APIOutageError: true
    ModelError: true
    VerificationError: false
    default: false
```

### `local_fallback` Section

```yaml
local_fallback:
  enabled: false                     # Must be true to use fallback
  provider: "ollama"
  url: ""                            # e.g., "http://192.168.2.243:11434"
  model: "qwen3.5:9b"
  context_window: 16384
  timeout: 300                       # HTTP timeout in seconds
  max_tokens: 4096
  allowed_operations:                # Whitelist (empty = allow all)
    - chat
    - summarization
    - simple_code
  blocked_operations:                # Blacklist (checked first)
    - review
    - security_review
    - planning
```

### Model Health Constants (in `model_health.py`)

| Constant | Value | Description |
|---|---|---|
| `_WINDOW_SIZE` | 20 | Sliding window size per model |
| `_CIRCUIT_BREAKER_THRESHOLD` | 5 | Consecutive failures to open circuit |
| `_CIRCUIT_RECOVERY_SECONDS` | 60 | How long circuit stays open |

---

## Troubleshooting

### Arc stuck in retry loop

**Symptoms**: Arc remains in `waiting` status indefinitely.

**Diagnosis**: Check `arc_state` for `_retry_count` and `_backoff_until`:
```sql
SELECT key, value_json FROM arc_state WHERE arc_id = ? AND key LIKE '\_%' ESCAPE '\';
```

**Fix**: Reset the arc to pending:
```sql
UPDATE arcs SET status = 'pending' WHERE id = ?;
DELETE FROM arc_state WHERE arc_id = ? AND key IN ('_retry_count', '_backoff_until');
```

### Fallback not activating

**Check**:
1. `local_fallback.enabled` is `true` in config
2. `local_fallback.url` is set and reachable
3. The operation type is in `allowed_operations` (or list is empty)
4. The operation type is NOT in `blocked_operations`
5. `tools` is `None` (fallback doesn't support tool calling)
6. Arc doesn't have `_fallback_allowed = false` in `arc_state`

### Circuit breaker stuck open

**Diagnosis**: Check model health:
```python
from carpenter.core.model_health import get_model_health
state = get_model_health("anthropic:claude-sonnet-4-5-20250929")
print(state.health, state.circuit_open_until)
```

**Fix**: Manual reset:
```python
from carpenter.core.model_health import reset_circuit_breaker
reset_circuit_breaker("anthropic:claude-sonnet-4-5-20250929")
```

### Resetting all circuit breakers

```python
from carpenter.core.model_health import get_all_model_health, reset_circuit_breaker
for state in get_all_model_health():
    if state.health.value == "circuit_open":
        reset_circuit_breaker(state.model_id)
```
