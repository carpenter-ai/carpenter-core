# Review Pipeline

All submitted code goes through the review pipeline before execution.

## Pipeline stages
1. **Hash check** — Previously approved identical code skips review
2. **AST parse** — Validates Python syntax
3. **Injection scan** — Checks for prompt injection patterns
4. **Sanitize** — Cleans unsafe constructs
5. **Reviewer AI call** — AI reviews code for security and alignment

## Adversarial mode
When `review.adversarial_mode` is enabled, the reviewer must find a minimum number of issues. This increases review thoroughness at the cost of more API calls.

## Related
[[security/trust-boundaries]] · [[chat/utilities]] · [[security/config]]
