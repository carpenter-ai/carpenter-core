# Coding-Time Verification

When the coding agent ends its turn, every file it touched is re-checked by a content-type-keyed verifier. If any check fails, finalization is rejected and the structured findings come back as a follow-up message. The agent must fix the file before it can end the turn.

This is a second line of defence in front of runtime trust enforcement. The runtime still rejects malformed batches at `create_arc()` / `create_batch()`. The verifier just catches the same mistakes earlier — at the file the agent is editing — instead of at the arc that fails to spawn.

## Where the rules live

- Framework: `carpenter/verify/registry.py` — `register_verifier(content_type, fn)`, `detect_content_type(path)`.
- YAML workflow templates: `carpenter/verify/yaml_template.py` — full rule list.
- Finalization hook: `carpenter/agent/coding_agent.py` `_verify_touched_files()`.

## YAML workflow templates

For files under `config_seed/templates/*.yaml`, the verifier rejects malformed trust topology. The rules mirror the runtime contract for `create_untrusted_batch`:

- Every step with `integrity_level: untrusted` must be an EXECUTOR.
- That EXECUTOR must have a downstream REVIEWER sibling (later `order`) with `reviewer_profile: security-reviewer` (or another registered reviewer profile) and `integrity_level: trusted`.
- That REVIEWER must have a downstream JUDGE sibling with `reviewer_profile: judge` and `integrity_level: trusted`.
- Untrusted EXECUTOR steps must pin `output_type: json`.
- A JUDGE step cannot itself be untrusted.

If any of those rules fail, the verifier returns an error finding with a line number and a fix hint. The agent must edit the template until the verifier passes.

## Worked example

Malformed (untrusted EXECUTOR with no reviewer/judge siblings):

```yaml
steps:
  - name: Fetch web content
    description: Fetch the URL.
    integrity_level: untrusted
    agent_type: EXECUTOR
    order: 0
```

Finding returned:

```
severity: error
line: 3
message: Untrusted EXECUTOR step "Fetch web content" has no downstream
         REVIEWER sibling with a registered reviewer_profile.
fix_hint: Add a REVIEWER step (order > 0) with reviewer_profile:
          security-reviewer and integrity_level: trusted, followed by a
          JUDGE step with reviewer_profile: judge.
```

Corrected:

```yaml
steps:
  - name: Fetch web content
    description: Fetch the URL.
    integrity_level: untrusted
    output_type: json
    agent_type: EXECUTOR
    order: 0
  - name: Review fetched content
    description: Extract the relevant information.
    agent_type: REVIEWER
    integrity_level: trusted
    reviewer_profile: security-reviewer
    order: 1
  - name: Validate review
    description: Validate the reviewer's extraction.
    agent_type: JUDGE
    integrity_level: trusted
    reviewer_profile: judge
    order: 2
```

## Adding a verifier for a new content type

1. Write a function `(content: str, context: dict | None) -> VerificationResult`.
2. Return `VerificationFinding(severity, line, message, fix_hint)` for each violation. `severity="error"` blocks finalization; `"warning"` is informational.
3. `fix_hint` should name the concrete next edit and link to the KB article that documents the rule — that text is what the coding agent reads.
4. Register: `register_verifier("my-type", my_verify_fn)`. Extend `detect_content_type()` so the finalization hook routes the right files to your verifier.

## Related
[[security/trust-boundaries]] · [[security/typed-declarations]] · [[security/review-pipeline]]
