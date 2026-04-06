# Review Pipeline Failures

Failures that occur during the security review pipeline (`review/pipeline.py`). The pipeline stages are: hash check, import-star check, AST parse, injection scan, sanitize, reviewer AI call, outcome determination.

---

## Injection False Positive

**Symptoms**: Code is flagged with advisory injection warnings (severity HIGH) even though the code is benign. The `PipelineResult` has `outcome = MAJOR` due to high-severity injection flags, and the code is not executed.

**Root cause**: The injection defense scanner (`review/injection_defense.py`) uses pattern matching to detect potential prompt injection in string literals and comments. Legitimate strings that happen to match injection patterns (e.g., strings containing "ignore previous instructions", "system prompt", or role markers like "assistant:") trigger false positives.

**Escape**:
1. Review the specific advisory flags in the `PipelineResult.advisory_flags` list to identify which strings triggered the alert.
2. Rewrite the code to avoid embedding the triggering text as string literals. Options:
   - Build the string dynamically (concatenation, f-strings with variables).
   - Store the content in a state variable first, then reference it.
   - Read the content from a file rather than embedding it inline.
3. If the content genuinely must contain injection-like patterns (e.g., you are writing documentation about prompt injection), explain this to the user and note that the current review pipeline will block it. The user may need to execute this manually.

**Prevention**: Avoid embedding large blocks of natural language text as Python string literals in submitted code. Use state variables or files for content that might contain patterns resembling instructions.

**Escalation**: If legitimate work is repeatedly blocked by injection false positives, inform the user. This is a known limitation of pattern-based injection defense.

---

## AST Parse Error (Syntax Error)

**Symptoms**: The `PipelineResult` has `status = "minor_concern"` and `outcome = REWORK` with a reason starting with "Syntax errors:". The code has invalid Python syntax.

**Root cause**: The submitted code is not valid Python. Common causes: unclosed brackets/strings, incorrect indentation, using syntax from a different Python version, incomplete code fragments, copy-paste errors.

**Escape**:
1. Read the specific syntax error from the `reason` field. It includes line number and error message.
2. Fix the syntax error at the indicated location.
3. Common fixes:
   - Missing closing parenthesis, bracket, or brace: count openers vs closers.
   - Unterminated string: check for mismatched quotes, especially triple quotes.
   - Indentation error: ensure consistent use of spaces (not tabs) and correct nesting.
   - f-string with nested quotes: use different quote types for inner/outer strings.
4. Resubmit the corrected code. The pipeline caches approved code hashes, so a previously-approved identical resubmission skips review.

**Prevention**: Before submitting code via `submit_code`, mentally verify the syntax is complete and correct. Pay special attention to string termination and bracket matching in multi-line code.

**Escalation**: Syntax errors are always fixable. No escalation needed unless the target Python version does not support the required syntax.

---

## Policy Violation: Import Star

**Symptoms**: The `PipelineResult` has `status = "rejected"` and `outcome = REJECTED` with reason "Policy violation: ... from X import *".

**Root cause**: The code contains a wildcard import (`from module import *`). This is a hard policy violation that is auto-rejected with no retry allowed. Wildcard imports are prohibited because they make it impossible to determine what names are being imported, which undermines the security review.

**Escape**:
1. Replace `from module import *` with explicit imports: `from module import name1, name2, name3`.
2. If you need many names from a module, import the module itself and use qualified access: `import module` then `module.name1`.
3. Resubmit with explicit imports.

**Prevention**: Never use `from X import *` in submitted code. Always use explicit imports.

**Escalation**: None. This is a hard policy rule with no exceptions.

---

## Reviewer Model Unavailable

**Symptoms**: The review pipeline raises an exception during the "Reviewer AI call" step (step 6). Error messages may include HTTP 500/502/503 from the AI provider, connection errors to the AI API, or `ValueError: No reviewer_model or chat_model configured`.

**Root cause**: The AI reviewer model configured in `review.reviewer_model` (or the fallback `chat_model`) is unreachable. This can happen because:
- The AI provider API is down or rate-limited.
- The API key is invalid or expired.
- The configured model name is incorrect.
- Network connectivity to the AI provider is lost.

**Escape**:
1. If the error is transient (503, rate limit), retry after a short delay. The invocation loop has built-in mechanical retries (`mechanical_retry_max`, default 4).
2. If the API key is invalid: inform the user to check their API key configuration.
3. If the model name is wrong: check `config.CONFIG.get("review", {}).get("reviewer_model")`. Common format is `"provider:model-name"` (e.g., `"anthropic:claude-sonnet-4-20250514"`).
4. If there is no reviewer model AND no chat model configured: this is a configuration error. The user must set at least one.
5. For Ollama-backed reviewers: check that the Ollama server at `config.CONFIG["ollama_url"]` is running and the model is pulled.

**Prevention**: Ensure both `review.reviewer_model` and `chat_model` are configured. Using a dedicated reviewer model (rather than falling back to chat_model) is recommended.

**Escalation**: If the AI provider is experiencing an outage, inform the user. Code review cannot proceed without the reviewer model.

---

## REWORK Loop Exhaustion

**Symptoms**: Code is submitted, receives `outcome = REWORK` (status `"minor_concern"`), the agent fixes the code and resubmits, and this cycle repeats multiple times without ever reaching `APPROVE`. The user sees repeated review notes.

**Root cause**: The review pipeline returns REWORK when the AI reviewer finds minor issues or when medium-risk patterns are detected. If the agent's fixes introduce new issues or fail to address the reviewer's concerns, the cycle continues. There is no built-in maximum on REWORK attempts at the pipeline level -- the limit comes from the chat tool iteration cap (`chat_tool_iterations`, default 10).

**Escape**:
1. After 2-3 failed REWORK cycles, stop and analyze the pattern. Read the reviewer's reason from each attempt.
2. If the reviewer keeps flagging the same issue: the approach may be fundamentally incompatible with the review policy. Consider a completely different implementation strategy.
3. If the reviewer flags different issues each time: the fixes are introducing new problems. Step back, write the complete correct code from scratch rather than patching incrementally.
4. If the reviewer's concerns seem unreasonable: explain the situation to the user. The reviewer model may be overly conservative for this particular task.
5. Check if the code is triggering injection defense false positives (see "Injection False Positive" above) -- these can cause repeated REWORK even when the code is correct.

**Prevention**: Write clean, minimal code that does exactly what is requested. Avoid unnecessary complexity, extra imports, or side effects that may concern the reviewer. Keep submitted code focused on a single task.

**Escalation**: After 3 REWORK cycles, inform the user about the pattern. They may need to adjust the reviewer model or manually approve the operation.

---

## Code Sanitization Failed

**Symptoms**: The `PipelineResult` has `status = "minor_concern"` and `outcome = REWORK` with reason starting with "Code sanitization failed:". The code passed syntax validation but failed during the sanitization step.

**Root cause**: The code sanitizer (`review/code_sanitizer.py`) strips string literals, renames variables, and removes comments to prevent prompt injection against the reviewer. Some valid Python constructs may cause the sanitizer to fail -- unusual AST node types, very complex nested expressions, or edge cases in the sanitizer's implementation.

**Escape**:
1. Read the specific sanitization error message.
2. Simplify the code structure. Break complex expressions into simpler statements.
3. Avoid unusual Python features (walrus operator in unusual positions, complex comprehensions with multiple conditions, deeply nested f-strings).
4. If the sanitizer consistently fails on a particular construct, rewrite that construct in a simpler form.

**Prevention**: Write straightforward Python. Avoid deeply nested expressions and unusual syntax patterns.

**Escalation**: If the code genuinely cannot be simplified and the sanitizer cannot handle it, this is a platform limitation. Inform the user.
