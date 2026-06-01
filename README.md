# Carpenter

Pure-Python AI agent platform using the CaMeL pattern.

## Quick Start

```bash
bash install.sh               # Interactive setup
python3 -m carpenter_linux    # Start server (via platform package)
# Open http://localhost:7842
```

## Development

### Running Tests

**Recommended (fast, parallel execution):**
```bash
pytest tests/ -n auto
```

This runs tests in parallel across all CPU cores, reducing runtime from ~5 minutes to ~1.5 minutes (72% faster).

**Alternative (single-threaded):**
```bash
pytest tests/
```

**Test Infrastructure:**
- Isolated temp databases per test via `test_db` fixture
- Database template caching for fast initialization

**Acceptance Tests:**
Acceptance stories live in `carpenter-linux` at `user_stories/`. Run them via `carpenter-dev-tools`:
```bash
cd ~/carpenter-dev-tools/acceptance
MODEL=haiku ./run-comprehensive-tests.sh
```

### Project Structure

See `CLAUDE.md` for comprehensive documentation including:
- Repository structure
- Architecture patterns
- Configuration guide
- Key patterns and conventions

## Documentation

All documentation lives in [`docs/`](docs/).

### Design

- [`docs/design.md`](docs/design.md) — **Authoritative system design document**
- [`docs/coding-invariants.md`](docs/coding-invariants.md) — Coding invariants the platform aims for (narrow platform / wide configuration, fail-closed security, audit, etc.)

### Engineering Reference

- [`docs/security-model.md`](docs/security-model.md) — Tool partitioning, review pipeline, adding new tools
- [`docs/trust-invariants.md`](docs/trust-invariants.md) — I1-I9 security invariants with enforcement and test pointers
- [`docs/coding-guidelines.md`](docs/coding-guidelines.md) — Rules for code submitted to the review pipeline
- [`docs/review-outcomes-reference.md`](docs/review-outcomes-reference.md) — The 5 review outcomes (CACHED, APPROVE, REWORK, MAJOR, REJECTED)
- [`docs/template-rigidity.md`](docs/template-rigidity.md) — Template-mandated arc immutability rules
- [`docs/verified-flow-analysis.md`](docs/verified-flow-analysis.md) — Design for static taint verification (not yet implemented)

### Operations

- [`docs/model-selection-guide.md`](docs/model-selection-guide.md) — Model registry, presets, adding custom models
- [`docs/retry-and-health.md`](docs/retry-and-health.md) — Error types, circuit breakers, troubleshooting

### Website

For conceptual documentation (architecture overview, trust model, security philosophy):
[carpenter-ai.org](https://carpenter-ai.org/)

## Features

- Pure-Python implementation
- Read-only agency + pythonic action security model
- Multi-provider AI support (cloud and local models)
- Model escalation and conversation management
- Knowledge base with searchable skill entries and graph links
- Conversation boundary memory and reflective meta-cognition
- Trust boundary system with arc-level isolation
- Web UI with HTMX chat interface
- Git workflow integration
- Connector system (web, Telegram, Signal) with file watcher support

## License

See LICENSE file for details.
