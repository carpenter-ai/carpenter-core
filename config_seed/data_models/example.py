"""Example data model for inter-arc communication."""
import attrs


@attrs.define
class TaskResult:
    status: str
    output: str | None = None
    error: str | None = None
    metrics: dict | None = None
