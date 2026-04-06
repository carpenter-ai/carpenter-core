# Available Models

AI models available for arc execution and planning. Declared in `config.yaml` under the `models:` key.

## Model Manifest

| Model | Cost | Best For | Avoid For |
|-------|------|----------|-----------|
| **opus** | high | Complex reasoning, architecture decisions, security review, multi-step planning | Mechanical/repetitive tasks where cost is wasted |
| **sonnet** | medium | Standard implementation, code review, general-purpose tasks | Tasks that are purely mechanical (use haiku) or require deepest reasoning (use opus) |
| **haiku** | low | Summarization, simple code generation, data extraction, formatting | Security review, complex architecture decisions, nuanced planning |

## Decision Table

| Task Type | Recommended Model | Rationale |
|-----------|-------------------|-----------|
| Security review | opus | Non-negotiable; templates enforce `model_min_tier: high` |
| Architecture planning | opus | Deep reasoning required for novel decomposition |
| Standard implementation | sonnet | Balanced cost/capability for most coding tasks |
| Code review (routine) | sonnet | Good enough for most review; opus for sensitive code |
| Summarization / formatting | haiku | Mechanical tasks; cost-optimize |
| Data extraction | haiku | Structured output from known formats |
| Routine planning | sonnet | Sufficient for standard decomposition |

## Accessing Model Info

- **Chat tool:** `list_models` returns the full manifest
- **In submitted code:** `carpenter_tools.read.config.models()` returns `{"models": {...}}`
- **In submitted code:** `carpenter_tools.read.config.get_value("models")` also works

## Assigning Models to Arcs

Pass `agent_model` when creating arcs:
```python
arc.add_child(parent_id, name="Review security", agent_model="opus")
arc.add_child(parent_id, name="Format output", agent_model="haiku")
```

Templates pre-assign models per step. Overrides must respect `model_min_tier`.

## Related

[[ai/config]] -- [[ai/providers]] -- [[arcs/planning]]
