# Security Configuration

## Review settings
- `review.adversarial_mode` — Require minimum findings in review (default: false)
- `review.adversarial_min_findings` — Minimum findings when adversarial mode is on (default: 1)

## Encryption
- `encryption.enforce` — Require Fernet encryption for tainted arc output (default: true, fails-closed)

## Network egress
- `egress_policy` — Executor network policy: auto, iptables, nftables, docker, none
- `egress_enforce` — When true and only noop available, log WARNING

## Allowlists
Under `security.*`: `email_allowlist`, `domain_allowlist`, `url_allowlist`, `filepath_allowlist`, `command_allowlist`

## Related
[[security/trust-boundaries]] · [[security/review-pipeline]] · [[self-modification/config-tools]]
