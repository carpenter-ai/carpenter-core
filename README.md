# carpenter-core

**[carpenter-ai.org](https://carpenter-ai.org/)** · [carpenter](https://github.com/carpenter-ai/carpenter) · [carpenter-linux](https://github.com/carpenter-ai/carpenter-linux)

The platform engine for Carpenter. This is where the security model lives.

Anything beyond a trustworthy read must be expressed as Python code and submitted for review. A separate reviewer LLM judges the code's structure — but never sees the data it operates on. String literals are sanitized out before review, so prompt injection can't smuggle actions past the reviewer. The reviewer sees logic, not payload.

This package provides:

- **Six-stage code review pipeline** — hash check, import check, AST parse, injection scan, histogram analysis, and sanitized review
- **Arc engine** — recursive work tree with planner, executor, reviewer, judge, and chat agents
- **Taint/trust system** — tracks trust boundaries across agent interactions, with a two-LLM firewall for untrusted output
- **Memory compression chain** — daily notes, weekly patterns, monthly insights, crystallized skills
- **Model selection** — YAML model registry with cost-aware routing and role-based presets

carpenter-core is platform-agnostic. Pair it with a platform package like [carpenter-linux](https://github.com/carpenter-ai/carpenter-linux) to run.

## License

[MIT](LICENSE)
