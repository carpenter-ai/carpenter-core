# Retries and Model Policies

How model selection, dispatch, and retries fit together. Pairs with
`retry-and-health.md` (low-level retry mechanics, error classification,
circuit breakers) and `model-selection-guide.md` (registry + scoring math).

---

## The `model_policies` table

Each arc has an optional `model_policy_id` referencing a row in
`model_policies`. A policy row is a small bundle of "how to pick / call a
model":

| Column         | Purpose                                                       |
|----------------|---------------------------------------------------------------|
| `name`         | Optional human-readable label (preset name, e.g. `fast-chat`) |
| `model`        | Hard-pinned model id (`provider:model`). NULL for selector    |
| `agent_role`   | Free-form role hint passed to invocation                      |
| `temperature`  | Sampling temperature override                                 |
| `max_tokens`   | Output token cap                                              |
| `policy_json`  | Serialized `ModelPolicy` (constraints + preference vector)    |

The two columns that matter for dispatch are `model` and `policy_json`:

- **Hard pin**: `model` set, `policy_json` ignored. The arc invokes that
  exact provider:model on every dispatch.
- **Selector**: `model` NULL, `policy_json` populated. The selector runs at
  dispatch time and returns a ranked list of eligible models.

This table replaces the old `agent_configs` table (dropped 2026-04-29).
The agent_configs schema only supported hard pins; `model_policies` is a
strict superset.

---

## Built-in presets

`carpenter/core/models/selector.py` defines four named policies. Operators
override individual fields via the `model_presets` config key.

| Preset             | Constraints                          | Preference (cost, quality, speed) | Use case                           |
|--------------------|--------------------------------------|-----------------------------------|------------------------------------|
| `fast-chat`        | `min_quality=2`                      | `(0.3, 0.2, 0.5)`                 | Interactive chat — favour latency  |
| `careful-coding`   | `min_quality=4`                      | `(0.1, 0.6, 0.3)`                 | Code generation / review           |
| `background-batch` | `max_cost_per_mtok_out=5.0`          | `(0.6, 0.2, 0.2)`                 | Bulk async work — minimise spend   |
| `caretaker`        | `max_quality=2`                      | `(0.5, 0.3, 0.2)`                 | Self-maintenance, log triage       |

Preference vectors are weights on the (cost, quality, speed) score axes;
they sum to 1.0 by convention.

---

## Dispatch-time resolution

`dispatch_model_resolver.resolve_dispatch_model(arc_id, arc_info)` is the
single entrypoint dispatch uses to turn `arc_info["model_policy_id"]` into
something callable. The control flow is:

1. **No policy_id** → returns `(None, [], None, False)`. Caller falls back
   to environment / agent-type defaults.
2. **Hard-pinned policy** (`policy_row.model` set) → returns the policy
   row dict as `model_config`, no fallbacks.
3. **Selector-driven policy** (`policy_json` set, `model` NULL) →
   deserialise `ModelPolicy.from_db_row`, run `select_models(policy)`
   against the registry. The top-ranked result becomes `model_config`,
   the remainder become `fallback_models` for failover.
4. **Empty ranked list** → return `connectivity_degraded=True`. Dispatch
   aborts and fires a connectivity event rather than invoking a model
   that the health system has blacklisted.

Selector results respect `model_health` circuit breakers, so a policy can
silently fall back to alternative models when one provider is degraded.

---

## How retries interact with policies

The retry layer (see `retry-and-health.md`) classifies errors and decides
whether to re-dispatch. Where policies fit in:

- **Within a single dispatch**, on failure the dispatch handler walks
  `fallback_models` (already produced by the selector) before giving up.
  No new policy lookup happens — the ranked list from the original
  resolution is reused.
- **Between dispatches**, on arc-level retry the resolver runs again
  against the current registry + health state, so a model that recovered
  in the meantime is re-eligible.
- **Hard-pinned policies have no fallback list.** Operators trading
  flexibility for predictability accept that retries on a pin loop on
  the same model until the per-error budget is exhausted.
- **Escalation** (root-arc failure with an escalation stack) creates a
  *new* `model_policies` row pointing at the next-tier model and starts
  a fresh sibling arc — it does not mutate the failed arc's policy.

---

## Operator troubleshooting

- **Arc keeps invoking the wrong model.** Look up the policy:
  `SELECT model, policy_json FROM model_policies WHERE id = ?`. If `model`
  is set, that's a hard pin — the selector is not consulted. If
  `policy_json` is set but a stale model is being chosen, the registry
  may be out of date or `model_health` is blacklisting the preferred
  models.
- **Selector returns nothing (`connectivity_degraded`).** Every
  registry model either fails constraints or is blacklisted. Check
  `model_health` rows; loosen `min_quality` / `max_cost_per_mtok_out`;
  or temporarily switch the arc to a hard pin.
- **Preset overrides not taking effect.** `get_presets()` reads
  `model_presets` from config on each call, but module-level `PRESETS`
  is captured once at import. Code paths using `PRESETS` directly need
  a server restart after config edits.
- **Verifying an arc's resolved model.** The dispatch handler logs
  `"Arc N: model selector chose X (reason), K fallback(s)"` at INFO. Grep
  `journalctl --user -u carpenter` for the arc id to see what was
  actually chosen and why.
