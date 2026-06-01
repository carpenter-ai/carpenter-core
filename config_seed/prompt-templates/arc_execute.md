**Execute Arc #{{ arc_id }}**

{{ goal }}

Use `submit_code` to accomplish this task. Submit code in a SINGLE call — do not use read tools or explore first, just submit the code.

The conversation_id ({{ source_conv_id }}) and arc_id ({{ arc_id }}) are auto-injected into the execution environment — you do NOT need to pass them explicitly.

## Typed string rules (REQUIRED)

`submit_code` runs through a verifier that REJECTS any bare string literal. Every `"..."` you pass as a value must be wrapped in a typed constructor imported from `carpenter_tools.declarations`:

- `Label("...")` — short identifiers, keys, status values, tags (no spaces, <=64 chars)
- `URL("...")` — http/https endpoints
- `Email("...")` — email addresses
- `WorkspacePath("...")` — file paths inside the workspace
- `SQL("...")` — database queries
- `JSON("...")` — JSON-encoded payloads
- `UnstructuredText("...")` — free-form prose, chat messages, goals, anything that isn't one of the above

Additional types include `Domain`, `FilePath`, `Command`, `IntRange`, `Enum`, `Pattern`. See `kb/security/typed-declarations.md` for the full list and guidance. Do not fight the allowlists — if a value you need is rejected, ask the platform to expand the allowlist rather than working around it.

Exempt (do NOT wrap): dict **keys** (`{"foo": Label("bar")}` — left side is fine bare), f-string interior fragments (`f"id={x}"` — wrap the whole f-string with `UnstructuredText(f"...")` if it's a value), keyword argument names, and import module names.

## Correct send-a-message pattern

```python
from carpenter_tools.act import messaging
from carpenter_tools.declarations import UnstructuredText
messaging.send(message=UnstructuredText("Your message content here"))
```

## Other correct examples

```python
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label, UnstructuredText
arc.create(name=Label("fetch-data"), goal=UnstructuredText("Pull today's metrics"))
```

```python
from carpenter_tools.act import files
from carpenter_tools.declarations import WorkspacePath, UnstructuredText
files.write(path=WorkspacePath("notes.txt"), content=UnstructuredText("hello"))
```

**When the arc is non-trusted (constrained or untrusted):**

Wrap every raw string in a typed declaration so the platform can track its provenance and enforce default-deny allowlists. For example:

```python
url = URL("https://example.com")
note = UnstructuredText("the page mentioned X")
label = Label("status_ok")
```

Do NOT explore or read files. Just write and submit the code immediately.
