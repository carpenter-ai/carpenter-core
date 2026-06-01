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

### Circuit breaker stuck open

**Diagnosis**: Check model health:
```python
from carpenter.core.model_health import get_model_health
state = get_model_health("anthropic:claude-sonnet-4-6")
print(state.health, state.circuit_open_until)
```

**Fix**: Manual reset:
```python
from carpenter.core.model_health import reset_circuit_breaker
reset_circuit_breaker("anthropic:claude-sonnet-4-6")
```

### Resetting all circuit breakers

```python
from carpenter.core.model_health import get_all_model_health, reset_circuit_breaker
for state in get_all_model_health():
    if state.health.value == "circuit_open":
        reset_circuit_breaker(state.model_id)
```
