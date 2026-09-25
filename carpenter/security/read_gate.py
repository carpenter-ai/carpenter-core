"""One gate for reads into an LLM context (I1, I2).

Untrusted or constrained content may reach a trusted LLM context only
through REVIEWER + JUDGE.  Read tools that hand stored content to an agent
therefore ask two questions, both answered here:

1. **Who is reading?**  :func:`reader_for` maps the platform-injected
   identity (conversation id, executor arc id) to a :class:`Reader` role.
   Tool input never supplies it.
2. **What is being read?**  The ``*_label`` functions give the integrity
   label of a stored item from its provenance: the arc that wrote it, the
   conversation it came from, the Resource row that owns it, or the
   ``file_provenance`` row of a file.

:func:`may_read` combines the two.  A trusted reader (chat, trusted
PLANNER / EXECUTOR arcs) may read only trusted items.  REVIEWER and JUDGE
readers, and non-trusted arcs, may read untrusted items: the REVIEWER is
chartered to extract from them, the JUDGE is platform code with no LLM
context, and a non-trusted arc's own output is already contained by the
review pipeline.

Labels are computed from provenance at read time, never stored as a
mutable flag, and every lookup fails closed: an unknown arc, a missing row
or a database error gives ``untrusted``.

**A REVIEWER arc is ``trusted`` by integrity level, but everything it
writes is labelled ``untrusted``.**  Its context holds the raw input it
reviews, so its final response, its state, its transcript and the files
it writes are text produced from untrusted bytes.  Only the JUDGE's
approval of its extract makes anything trusted (I3).

Refusals never echo the withheld bytes, and each one is written to
``trust_audit_log`` as ``trusted_read_refused``.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..db import db_connection

logger = logging.getLogger(__name__)

TRUSTED = "trusted"
UNTRUSTED = "untrusted"

ROLE_TRUSTED = "trusted"
ROLE_REVIEWER = "reviewer"
ROLE_JUDGE = "judge"
ROLE_UNTRUSTED = "untrusted"

AUDIT_EVENT_REFUSED = "trusted_read_refused"

# Arc working conversations are titled "[Arc #N] <goal>" by
# ``dispatch_handler._run_arc_agent``.  New ones are also tainted at
# creation (see ``taint_source_for_arc``); the title covers conversations
# created before that.
_ARC_TITLE_RE = re.compile(r"^\[Arc #(\d+)\]")


@dataclass(frozen=True)
class Label:
    """Integrity label of a stored item, with a platform-written reason."""

    level: str
    reason: str

    @property
    def trusted(self) -> bool:
        return self.level == TRUSTED


@dataclass(frozen=True)
class Reader:
    """The context a read would land in."""

    role: str
    conversation_id: int | None = None
    arc_id: int | None = None


_TRUSTED_LABEL = Label(TRUSTED, "trusted")


def join(*labels: Label | None) -> Label | None:
    """Return the least trusted of ``labels`` (``None`` entries ignored).

    Returns ``None`` only if every entry is ``None``.
    """
    present = [lab for lab in labels if lab is not None]
    if not present:
        return None
    for lab in present:
        if not lab.trusted:
            return lab
    return present[0]


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

# The agent invocation a chat tool is running for, set by the platform
# around every chat tool call (``invocation._execute_chat_tool``).  Core
# checks fall back on it when a caller does not pass the identity, so a
# tool module that predates this gate still reads with the right role.
_invocation: contextvars.ContextVar[tuple[int | None, int | None] | None] = (
    contextvars.ContextVar("read_gate_invocation", default=None)
)


@contextlib.contextmanager
def invocation_context(
    conversation_id: int | None, executor_arc_id: int | None,
):
    """Record the platform identity of the running chat tool call."""
    token = _invocation.set((conversation_id, executor_arc_id))
    try:
        yield
    finally:
        _invocation.reset(token)


def current_arc_id() -> int | None:
    """Executor arc of the running chat tool call, or None (chat, or none)."""
    ctx = _invocation.get()
    return ctx[1] if ctx else None


def current_conversation_id() -> int | None:
    """Conversation of the running chat tool call, or None."""
    ctx = _invocation.get()
    return ctx[0] if ctx else None


def reader_for(
    *, conversation_id: int | None = None, arc_id: int | None = None,
) -> Reader:
    """Return the reader for a platform-injected identity.

    No arc means the chat agent, which is trusted.  An arc id that does
    not resolve is treated as a trusted reader, the most restrictive role.
    """
    role = ROLE_TRUSTED
    if arc_id is not None:
        row = _arc_row(arc_id)
        if row is not None:
            integrity = row["integrity_level"] or TRUSTED
            agent_type = row["agent_type"] or "EXECUTOR"
            if integrity != TRUSTED:
                role = ROLE_UNTRUSTED
            elif agent_type == "REVIEWER":
                role = ROLE_REVIEWER
            elif agent_type == "JUDGE":
                role = ROLE_JUDGE
    return Reader(role=role, conversation_id=conversation_id, arc_id=arc_id)


def may_read(reader: Reader, label: Label | None) -> bool:
    """Return True iff ``reader`` may see an item labelled ``label``.

    ``None`` means the item has no label (content the platform does not
    track), which reads freely as before.
    """
    if label is None or label.trusted:
        return True
    return reader.role != ROLE_TRUSTED


# ---------------------------------------------------------------------------
# Item labels
# ---------------------------------------------------------------------------

def _arc_row(arc_id: int):
    try:
        with db_connection() as db:
            return db.execute(
                "SELECT id, integrity_level, agent_type FROM arcs WHERE id = ?",
                (int(arc_id),),
            ).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        logger.warning("read_gate: arc lookup failed for %r", arc_id, exc_info=True)
        return None


def arc_label(arc_id: int | None) -> Label:
    """Label of text an arc's agent wrote: its ``_agent_response``, its
    state values, its history content and the files it writes.

    ``untrusted`` for a non-trusted arc and for a REVIEWER.  JUDGE arcs
    run platform code and are trusted.  Unknown arcs are untrusted.
    """
    if arc_id is None:
        return Label(UNTRUSTED, "no arc")
    row = _arc_row(arc_id)
    if row is None:
        return Label(UNTRUSTED, f"unknown arc #{arc_id}")
    integrity = row["integrity_level"] or TRUSTED
    if integrity != TRUSTED:
        return Label(UNTRUSTED, f"{integrity} arc #{arc_id}")
    if (row["agent_type"] or "EXECUTOR") == "REVIEWER":
        return Label(UNTRUSTED, f"REVIEWER arc #{arc_id}")
    return _TRUSTED_LABEL


def taint_source_for_arc(arc_id: int) -> str | None:
    """Return the ``conversation_taint.source_tool`` value for an arc whose
    working conversation starts untrusted, or None for a trusted arc."""
    label = arc_label(arc_id)
    if label.trusted:
        return None
    return f"arc-context:{label.reason}"


def conversation_label(conversation_id: int | None) -> Label:
    """Label of a conversation's messages and tool calls.

    ``untrusted`` if the conversation has a ``conversation_taint`` row, or
    if it is the working conversation of an arc whose label is untrusted.
    """
    if conversation_id is None:
        return _TRUSTED_LABEL
    try:
        with db_connection() as db:
            taint = db.execute(
                "SELECT 1 FROM conversation_taint WHERE conversation_id = ? "
                "LIMIT 1",
                (conversation_id,),
            ).fetchone()
            row = db.execute(
                "SELECT title FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
    except sqlite3.Error:
        logger.warning(
            "read_gate: conversation lookup failed for %r", conversation_id,
            exc_info=True,
        )
        return Label(UNTRUSTED, f"conversation #{conversation_id} (lookup failed)")
    if taint is not None:
        return Label(UNTRUSTED, f"tainted conversation #{conversation_id}")
    title = (row["title"] if row is not None else None) or ""
    m = _ARC_TITLE_RE.match(title)
    if m:
        lab = arc_label(int(m.group(1)))
        if not lab.trusted:
            return Label(
                UNTRUSTED,
                f"conversation #{conversation_id} of {lab.reason}",
            )
    return _TRUSTED_LABEL


def check_conversation(
    reader: Reader, conversation_id: int | None, what: str,
) -> str | None:
    """Gate a read of a conversation's messages or tool calls.

    A context may always read its own conversation: that content is
    already in it.  Otherwise the conversation's label decides.
    """
    if conversation_id is not None and conversation_id == reader.conversation_id:
        return None
    return check(reader, what, conversation_label(conversation_id))


def state_label(arc_id: int | None, value) -> Label:
    """Label of an ``arc_state`` value.

    Arc 0 is conversation-level state written by the platform.  Its
    values are trusted except the withheld output of a tainted
    ``submit_code`` run, which is stored there flagged ``_tainted``.
    Every other arc's values carry the arc's label.
    """
    if arc_id in (0, None):
        if isinstance(value, dict) and value.get("_tainted"):
            return Label(UNTRUSTED, "withheld output of a tainted execution")
        return _TRUSTED_LABEL
    return arc_label(arc_id)


def agent_response_for(
    reader: Reader, arc_id: int,
) -> tuple[str, Label | None]:
    """Return the ``_agent_response`` a reader may see for ``arc_id``.

    The response is chosen as the completion notice always has: the arc's
    own, else its newest child's that has one.  If the reader may not see
    that response it is withheld; an older sibling's text is not offered
    in its place, since it would pass for the arc's result.

    Returns ``(text, None)`` when readable, ``("", label)`` when withheld,
    and ``("", None)`` when there is no response at all.
    """
    from ..core.arcs import manager as arc_manager
    from ..core.workflows._arc_state import get_arc_state

    candidates = [arc_id] + [
        c["id"] for c in reversed(arc_manager.get_children(arc_id) or [])
    ]
    for aid in candidates:
        text = get_arc_state(aid, "_agent_response", "") or ""
        if not text:
            continue
        label = arc_label(aid)
        if may_read(reader, label):
            return text, None
        audit_refusal(reader, f"_agent_response of arc #{aid}", label)
        return "", label
    return "", None


def context_label(
    conversation_id: int | None, executor_arc_id: int | None,
) -> Label:
    """Label of anything an agent writes from this context."""
    labels = [conversation_label(conversation_id)]
    if executor_arc_id is not None:
        labels.append(arc_label(executor_arc_id))
    return join(*labels) or _TRUSTED_LABEL


def execution_label(execution_id: int) -> Label:
    """Label of a code execution's output (its log)."""
    try:
        with db_connection() as db:
            row = db.execute(
                "SELECT ce.taint_source, cf.arc_id FROM code_executions ce "
                "JOIN code_files cf ON ce.code_file_id = cf.id "
                "WHERE ce.id = ?",
                (execution_id,),
            ).fetchone()
    except sqlite3.Error:
        return Label(UNTRUSTED, f"execution #{execution_id} (lookup failed)")
    if row is None:
        return Label(UNTRUSTED, f"unknown execution #{execution_id}")
    if row["taint_source"]:
        return Label(UNTRUSTED, f"tainted execution #{execution_id}")
    if row["arc_id"] is not None:
        lab = arc_label(row["arc_id"])
        if not lab.trusted:
            return Label(UNTRUSTED, f"execution #{execution_id} of {lab.reason}")
    return _TRUSTED_LABEL


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def tool_output_dir() -> str:
    """Directory holding full copies of truncated tool results."""
    code_dir = config.CONFIG.get("code_dir", "")
    base_data_dir = (
        str(Path(code_dir).parent) if code_dir
        else os.path.expanduser("~/carpenter/data")
    )
    return os.path.join(base_data_dir, "tool_output")


def _under(path: str, root: str | None) -> str | None:
    """Return ``path`` relative to ``root`` if it lies inside it."""
    if not root:
        return None
    root = os.path.realpath(os.path.expanduser(root))
    if path == root:
        return ""
    if path.startswith(root + os.sep):
        return path[len(root) + 1:]
    return None


def _resource_label_for_row(row) -> Label:
    from ..core.resources.trust import resource_trust
    rid = row["id"]
    if resource_trust(dict(row)) == TRUSTED:
        return _TRUSTED_LABEL
    return Label(UNTRUSTED, f"untrusted Resource #{rid}")


def _code_file_label(db, candidates: tuple[str, ...]) -> Label | None:
    row = db.execute(
        f"SELECT id, arc_id FROM code_files WHERE file_path IN "
        f"({','.join('?' * len(candidates))}) LIMIT 1",
        candidates,
    ).fetchone()
    if row is None:
        return None
    if row["arc_id"] is not None:
        lab = arc_label(row["arc_id"])
        if not lab.trusted:
            return Label(UNTRUSTED, f"code file of {lab.reason}")
    return _TRUSTED_LABEL


def _log_file_label(db, real: str, candidates: tuple[str, ...]) -> Label | None:
    """Label of an execution log, or None if no code execution owns it."""
    rows = db.execute(
        f"SELECT id FROM code_executions WHERE log_file IN "
        f"({','.join('?' * len(candidates))})",
        candidates,
    ).fetchall()
    labels: list[Label | None] = [execution_label(r["id"]) for r in rows]
    # The log path mirrors the code file's path (code_manager.execute),
    # and ``log_file`` is only stored once the run ends, so also resolve
    # the owning code file from the path.
    rel = _under(real, config.CONFIG.get("log_dir"))
    code_dir = config.CONFIG.get("code_dir")
    if rel and code_dir and rel.endswith(".log"):
        rel_stem = rel[: -len(".log")]
        code_paths = tuple(dict.fromkeys(
            os.path.join(root, rel_stem) + ext
            for root in (code_dir, os.path.realpath(code_dir))
            for ext in (".py", "")
        ))
        cf = db.execute(
            f"SELECT id FROM code_files WHERE file_path IN "
            f"({','.join('?' * len(code_paths))})",
            code_paths,
        ).fetchone()
        if cf is not None:
            labels.append(_code_file_label(db, code_paths))
            for r in db.execute(
                "SELECT id FROM code_executions WHERE code_file_id = ? "
                "AND taint_source IS NOT NULL AND taint_source != ''",
                (cf["id"],),
            ).fetchall():
                labels.append(execution_label(r["id"]))
    return join(*labels)


def path_label(path: str) -> Label | None:
    """Label of a file, or None if the platform does not track it.

    Sources, least trusted wins:

    - a ``resources`` row whose ``file_path`` is this file, or the Resource
      store directory (``<store>/<resource_id>/...``): the Resource's
      derived trust.  An unparseable or unknown id there is untrusted.
    - the truncated-tool-output directory: the ``file_provenance`` row
      written with the file; no row means untrusted.
    - code files and execution logs: the owning arc's label and the
      execution's taint.
    - per-arc workspaces (``<workspaces_dir>/arc-<id>/``,
      ``<base_dir>/data/state/<id>/``): the arc's label, whether or not
      the file has a provenance row (a git clone writes none).
    - any ``file_provenance`` row.
    """
    try:
        return _path_label(path)
    except Exception:  # noqa: BLE001 — classification must fail closed
        logger.warning("read_gate: path classification failed for %r", path,
                       exc_info=True)
        return Label(UNTRUSTED, "unclassifiable path")


def _path_label(path: str) -> Label | None:
    expanded = os.path.expanduser(path)
    real = os.path.realpath(expanded)
    candidates = tuple(dict.fromkeys(
        (real, os.path.normpath(os.path.abspath(expanded)), path)
    ))
    placeholders = ",".join("?" * len(candidates))
    labels: list[Label | None] = []

    from ..core.resources.manager import resource_storage_dir

    with db_connection() as db:
        for row in db.execute(
            f"SELECT * FROM resources WHERE file_path IN ({placeholders})",
            candidates,
        ).fetchall():
            labels.append(_resource_label_for_row(row))

        rel = _under(real, str(resource_storage_dir()))
        if rel:
            first = rel.split(os.sep, 1)[0]
            row = None
            if first.isdigit():
                row = db.execute(
                    "SELECT * FROM resources WHERE id = ?", (int(first),),
                ).fetchone()
            if row is None:
                labels.append(Label(UNTRUSTED, "Resource store file"))
            else:
                labels.append(_resource_label_for_row(row))

        prov = db.execute(
            f"SELECT writer_arc_id, writer_integrity_level FROM file_provenance "
            f"WHERE path IN ({placeholders})",
            candidates,
        ).fetchall()
        for p in prov:
            if p["writer_integrity_level"] != TRUSTED:
                labels.append(Label(
                    UNTRUSTED,
                    f"file written by {p['writer_integrity_level']} "
                    f"context (arc #{p['writer_arc_id']})",
                ))
            else:
                labels.append(_TRUSTED_LABEL)

        if _under(real, tool_output_dir()) is not None and not prov:
            labels.append(Label(UNTRUSTED, "tool output with no recorded context"))

        if _under(real, config.CONFIG.get("code_dir")):
            labels.append(_code_file_label(db, candidates))
        if _under(real, config.CONFIG.get("log_dir")):
            labels.append(_log_file_label(db, real, candidates))

    ws = _under(real, config.CONFIG.get("workspaces_dir"))
    if ws:
        m = re.match(r"arc-(\d+)(?:/|$)", ws)
        if m:
            labels.append(arc_label(int(m.group(1))))
    base_dir = config.CONFIG.get("base_dir")
    if base_dir:
        st = _under(real, os.path.join(base_dir, "data", "state"))
        if st:
            first = st.split(os.sep, 1)[0]
            if first.isdigit():
                labels.append(arc_label(int(first)))

    return join(*labels)


def record_file_label(
    path: str, label: Label, writer_arc_id: int | None = None,
) -> None:
    """Record ``label`` as the provenance of a platform-written file."""
    import datetime
    from ..db import db_transaction
    real = os.path.realpath(path)
    try:
        with db_transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO file_provenance "
                "(path, writer_arc_id, writer_integrity_level, written_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    real,
                    int(writer_arc_id) if writer_arc_id is not None else 0,
                    label.level,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )
    except sqlite3.Error:
        # No row means untrusted for tool output, so a failed write
        # fails closed.
        logger.warning("read_gate: could not record label for %s", real,
                       exc_info=True)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def audit_refusal(reader: Reader, what: str, label: Label) -> None:
    """Write a ``trusted_read_refused`` row.  Never raises."""
    try:
        from ..core.trust.audit import log_trust_event
        log_trust_event(reader.arc_id, AUDIT_EVENT_REFUSED, {
            "item": what,
            "label": label.level,
            "reason": label.reason,
            "reader_role": reader.role,
            "reader_conversation_id": reader.conversation_id,
        })
    except Exception:  # noqa: BLE001 — audit must not block the refusal
        logger.warning("read_gate: audit write failed", exc_info=True)


def withheld(what: str, label: Label) -> str:
    """Standard text shown in place of withheld content.

    Built only from platform-written fields: never the withheld bytes.
    """
    return (
        f"[{what} withheld: it comes from an untrusted context "
        f"({label.reason}). A trusted context may not read it. To use it, "
        "review it with a REVIEWER + JUDGE batch.]"
    )


def check(reader: Reader, what: str, label: Label | None) -> str | None:
    """Return None if the read is allowed, else the refusal text (audited)."""
    if may_read(reader, label):
        return None
    audit_refusal(reader, what, label)
    return withheld(what, label)
