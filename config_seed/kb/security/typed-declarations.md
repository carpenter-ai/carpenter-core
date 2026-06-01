# Typed String Declarations

In **non-trusted code** (code submitted by arcs at integrity level `constrained` or `untrusted`), every bare string literal is rejected by the platform's verification step. Each string must be wrapped in a typed declaration so the platform can track its provenance and check it against default-deny allowlists.

## Why

- **Taint propagation.** A bare string is opaque to the platform — there is no way to tell whether it originated as a trusted hardcoded constant, was extracted from an untrusted document, or was synthesised by an AI from tainted context. Wrapping in a typed constructor records intent at the call site.
- **Default-deny policy literals.** Policy types (`Email`, `Domain`, `Url`, `FilePath`, `Command`, `IntRange`, `Enum`, `Pattern`) validate their input against an allowlist that defaults to **empty = deny everything**. This is the platform's hard line on what untrusted code is allowed to construct.
- **Sanitization-friendly review.** The reviewer AI sees the constructor name (`Label`, `URL`, …) but never the raw bytes of the literal — the structure is reviewable while the attacker-controlled content stays out of the reviewer's context.
- **The type IS the classifier.** Wrapping every string at its source gives each value an explicit provenance label so taint tracking and allowlist checks can route it correctly — no heuristic inference needed. Unlabelled strings would be indistinguishable from attacker-controlled text once they flow through tools.

## Available types

**SecurityType declarations** (from `carpenter_tools.declarations`) — describe the role of the string:

- `Label(...)` — short structural identifier (status names, enum-like tags, keys)
- `URL(...)` — http/https endpoints (subject to URL allowlist)
- `Email(...)` — email addresses (format-validated; subject to email/domain allowlist)
- `UnstructuredText(...)` — free-form prose known to be untrusted (always passes; routed to progressive review)
- `WorkspacePath(...)` — a path inside the arc's workspace (no `..`)
- `SQL(...)` — database queries (allowed keyword, parameterised)
- `JSON(...)` — pre-shaped structured payloads (must parse)

**PolicyLiteral types** — validated against a default-deny allowlist:

- `Domain`, `Url`, `FilePath`, `Command`
- `IntRange`, `Enum`, `Bool`, `Pattern`
- `EmailPolicy`

## Example

```python
from carpenter_tools.declarations import URL, UnstructuredText, Label

target = URL("https://example.com/api")
note = UnstructuredText("page mentioned X")
status = Label("status_ok")
```

## Violation → fix examples

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

## Do not fight the allowlist

If a value you need is rejected by the policy type, that's a deliberate platform decision: **the allowlist defaults to deny**, and only the platform (under human review) can expand it. Do not try to work around the check by reshaping the string, encoding it, or routing it through `UnstructuredText`. Ask the platform to expand the relevant allowlist instead — the request itself becomes part of the audit trail.

## Related
[[security/trust-boundaries]] · [[security/review-pipeline]]
