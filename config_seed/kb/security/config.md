# Security Configuration

## Review settings
- `review.adversarial_mode` — Require minimum findings in review (default: false)
- `review.adversarial_min_findings` — Minimum findings when adversarial mode is on (default: 1)

## Encryption
- `encryption.enforce` — Require Fernet encryption for tainted arc output (default: true, fails-closed)

## Network egress
- Executor code cannot open network connections itself (the restricted executor refuses network imports); all network access goes through tool callbacks. This is not configurable.
- `web_egress_allowlist` — Web tools (`web.*`) reach any public address, and refuse loopback, private, link-local and other non-global destinations unless listed here (exact hostnames, or CIDRs matched against resolved addresses).

## Allowlists
Under `security.*`: `email_allowlist`, `domain_allowlist`, `url_allowlist`, `filepath_allowlist`, `command_allowlist`. These seed the JUDGE's default-deny policy allowlists: they decide which *values* CONSTRAINED data may take, not which hosts are reachable. Adding a domain here does not let web tools reach it, and `web_egress_allowlist` does not make a value pass a JUDGE.

## Related
[[security/trust-boundaries]] · [[security/review-pipeline]] · [[self-modification/config-tools]]
