# Typed String Declarations

Every string literal in submitted code must be wrapped in a platform-provided type constructor. Bare string literals are rejected by the `PROFILE_STEP` verifier.

## Why

The type IS the classifier. Wrapping every string at its source gives each value an explicit provenance label so taint tracking and allowlist checks can route it correctly — no heuristic inference needed. Unlabelled strings would be indistinguishable from attacker-controlled text once they flow through tools.

## Recognized constructors

**SecurityType** (from `carpenter_tools.declarations`):
- `Label(...)` — keys, status values, short identifiers
- `Email(...)` — email addresses (format-validated)
- `URL(...)` — http/https endpoints
- `WorkspacePath(...)` — paths inside the workspace (no `..`)
- `SQL(...)` — database queries (allowed keyword, parameterised)
- `JSON(...)` — structured data (must parse)
- `UnstructuredText(...)` — free-form prose (always passes; routed to progressive review)

**PolicyLiteral** (allowlist-checked):
- `EmailPolicy`, `Domain`, `Url`, `FilePath`, `Command`, `IntRange`, `Enum`, `Bool`, `Pattern`

## Examples

Violation:
```
msg = "task complete"
```
Fix:
```
msg = Label("task complete")
```

Violation:
```
greeting = f"Hello {name}"
```
Fix (choose the type that matches the content):
```
greeting = UnstructuredText(f"Hello {name}")
```

Violation:
```
q = "SELECT * FROM users WHERE id = ?"
```
Fix:
```
q = SQL("SELECT * FROM users WHERE id = ?")
```

## Exempt cases — do NOT wrap these

The verifier already skips these; wrapping them is unnecessary and often wrong:
- **Dict keys** in dict literals (`{"status": Label("ok")}` — `"status"` is a structural identifier)
- **F-string inner fragments** (`f"prefix-{x}"` — the literal `"prefix-"` inside is part of the f-string; wrap the whole f-string instead)
- **Format specs** inside `FormattedValue`
- **Import module names** (`from x import y`)
- **Keyword argument names** (`func(name="x")` — `name` is an identifier, not a string node)

## Related
[[security/trust-boundaries]] · [[security/review-pipeline]]
