# Scheduling Configuration

Cron entries are stored in the `cron_entries` database table.

## Key config
- `heartbeat_seconds` — How often the platform checks for due triggers (default: 5)

## Cron expression format
Standard 5-field cron: `minute hour day month weekday`

Examples:
- `0 9 * * *` — Every day at 9 AM
- `*/15 * * * *` — Every 15 minutes
- `0 23 * * 0` — Sundays at 11 PM

## Related
[[scheduling/tools]] · [[self-modification/config-tools]]
