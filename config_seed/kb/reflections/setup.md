# Reflection setup

Reflections are autonomous periodic reviews (daily cadence) that scan
recently-completed root arcs and propose kb/skill/doc changes via
`coding-change` follow-up arcs. Because they fire from a cron with no
originating chat conversation, any human-review URLs their follow-up
arcs produce have no chat medium to be delivered through.

To close that gap, reflection is **gated on a configured escalation
destination**. The daily tick (`reflection.daily_tick`) refuses to run
until both of the following are configured:

1. `reflection.escalation.email.to` — a valid destination address in
   the platform config (e.g. `reflection: { escalation: { email: { to:
   "you@example.com" } } }` in `config.yaml` or the equivalent env override).
2. SMTP credentials for the `carpenter-imap-email` package, resolved via
   the platform's per-package credential store at
   `/media/jabenta/carpenter/data/config/packages/carpenter-imap-email/.env`.
   Required keys: `EMAIL_SMTP_HOST`, `EMAIL_SMTP_USERNAME`,
   `EMAIL_SMTP_PASSWORD`. Optional: `EMAIL_SMTP_PORT` (default 587) and
   `EMAIL_SMTP_FROM` (defaults to `EMAIL_SMTP_USERNAME`).

When both are configured, each daily tick creates a virtual conversation
titled "Reflection escalation" with `channel_type='email'`, links every
reflection SUPERVISOR arc to it, and routes reflection completion messages
(including any pending review URLs) through SMTP to the configured address.
Existing arc→conversation-notify code is unchanged; only the delivery
side of `conversation.add_message` was generalised to route email-medium
conversations through SMTP.

**Yolo mode** — running reflections without any escalation destination
(so their proposed changes auto-apply without review) is not a
reflection-specific setting. It is controlled per content type via the
existing security-policy permissions on `coding-change` (T0/T1/T2
tiers). If you want reflections to actually run but their outputs to
apply without human review, adjust the per-tier auto-approval policy —
do not remove the escalation destination.

See also: [[reflections]].
