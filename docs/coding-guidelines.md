# Carpenter Coding Guidelines

Guidelines for code submitted to Carpenter's security review pipeline.

## Import Rules

### Wildcard Imports Prohibited

**Rule**: Do NOT use wildcard imports (`from module import *`)

**Rationale**: Wildcard imports:
- Obscure what names are being imported into the namespace
- Can be used for obfuscation in malicious code
- Make code review difficult (reviewer cannot see what's available)
- Violate Python best practices (PEP 8)

**Examples**:

❌ **INVALID** (auto-rejected):
```python
from os import *
from .helpers import *
from my_module import *
```

✅ **VALID**:
```python
from os import path, environ
from .helpers import calculate_hash, format_output
import my_module
```

**Enforcement**: Code containing wildcard imports will be automatically rejected by the review pipeline with no retry attempts. This is a policy violation, not a fixable error.

## String-Based Indirection Patterns

### High-Risk Patterns

Avoid using string literals to reference code entities, especially when combined with dynamic invocation:

❌ **AVOID** (flagged for review):
```python
# Indirect function call via getattr
getattr(module, "dangerous_function")()

# Dynamic imports via string
importlib.import_module("malicious_payload")
__import__("untrusted_module")

# Code execution from strings
exec("untrusted_code()")
eval("user_input")

# Dictionary-based name resolution
globals()["hidden_function"]()
locals()["obfuscated_var"] = value
```

✅ **PREFER** (direct references):
```python
# Direct function calls
module.dangerous_function()

# Standard imports
import malicious_payload
from untrusted_module import specific_function

# Avoid exec/eval entirely when possible
# If necessary, use literal code strings only
```

**Note**: The review pipeline will flag string-based indirection patterns and may require human review even if automated checks pass. Legitimate uses (plugin systems, dynamic configuration) should include clear comments explaining the purpose.

## Multi-File Changes

### Import Consistency

When submitting multi-file changes:
- Ensure all imports reference files included in the changeset
- Update import statements if you rename files
- Avoid circular dependencies

**Example**:

If you rename `helper.py` to `utils.py`, update all files that import it:

```python
# Before
from .helper import process_data

# After (when helper.py renamed to utils.py)
from .utils import process_data
```

### Cross-File References

When one file references names defined in another:
- Use direct imports, not string-based indirection
- Keep the dependency graph clear
- Document cross-file dependencies in comments when complex

## General Security Principles

1. **Explicit is better than implicit** - Don't obscure what your code does
2. **Fail safe** - If your code has an error path, fail visibly rather than silently
3. **Minimize privileges** - Don't request more access than you need
4. **Document intent** - Complex operations should have comments explaining why

## Review Process

Code submitted via `submit_code` goes through:
1. **Syntax validation** - Must be valid Python
2. **Import star check** - Auto-rejected if found (no retry)
3. **Static analysis** - Pattern matching for known risks
4. **Sanitization** - Variable/string renaming for blind review
5. **AI review** - Intent alignment check
6. **Human approval** - Final decision on execution

### Retry Policy

- **REWORK**: Agent can retry up to 3 times (covers syntax errors, minor logic issues, style problems)
- **MAJOR**: Requires human decision or plan expansion
- **REJECTED**: Policy violation, no retry (fix the violation or abandon the approach)

### Escalation

If the coding agent fails to satisfy the reviewer after 3 attempts, the task is automatically escalated to **MAJOR** status requiring human intervention.

## Questions?

If these guidelines prevent you from accomplishing a legitimate task, discuss it with the user. Some restrictions (like import *) are absolute, but others may have exceptions for specific use cases.
