"""Chat tools for code execution and API call introspection."""

import json
import logging

from carpenter.chat_tool_loader import chat_tool
from carpenter import config

logger = logging.getLogger(__name__)


@chat_tool(
    description=(
        "List recent tool calls from your own history. Shows tool name, input "
        "summary, result preview, duration, and timestamp. Use to review what "
        "tools you used and what happened."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "integer",
                "description": "Filter to a specific conversation. Omit for all.",
            },
            "tool_name": {
                "type": "string",
                "description": "Filter by tool name (e.g. 'read_file'). Omit for all.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 20).",
            },
        },
        "required": [],
    },
    capabilities=["database_read"],
)
def list_tool_calls(tool_input, **kwargs):
    from carpenter.db import get_db
    db = get_db()
    try:
        conditions = []
        bind_vals = []
        if tool_input.get("conversation_id"):
            conditions.append("tc.conversation_id = ?")
            bind_vals.append(tool_input["conversation_id"])
        if tool_input.get("tool_name"):
            conditions.append("tc.tool_name = ?")
            bind_vals.append(tool_input["tool_name"])
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        limit = tool_input.get("limit", 20)
        bind_vals.append(limit)
        rows = db.execute(
            f"SELECT tc.id, tc.conversation_id, tc.tool_use_id, tc.tool_name, "
            f"tc.input_json, tc.result_text, tc.duration_ms, tc.created_at "
            f"FROM tool_calls tc {where} ORDER BY tc.id DESC LIMIT ?",
            tuple(bind_vals),
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return "No tool calls found."
    lines = []
    for r in rows:
        input_preview = (r["input_json"] or "")[:80]
        result_preview = (r["result_text"] or "")[:80]
        dur = f" ({r['duration_ms']}ms)" if r["duration_ms"] is not None else ""
        lines.append(
            f"#{r['id']} conv={r['conversation_id']} {r['tool_name']}{dur}  "
            f"({r['created_at']})\n"
            f"  input: {input_preview}\n"
            f"  result: {result_preview}"
        )
    return "\n".join(lines)


@chat_tool(
    description=(
        "List recent code executions (from submit_code and arc steps). Shows "
        "code file path, source, execution status, exit code, and timestamps. "
        "Use to review what code was run and whether it succeeded."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "Filter by execution status: success, failed, timed_out, error. "
                    "Omit for all."
                ),
            },
            "source": {
                "type": "string",
                "description": (
                    "Filter by code source: chat_agent, agent, user. Omit for all."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 20).",
            },
        },
        "required": [],
    },
    capabilities=["database_read"],
)
def list_code_executions(tool_input, **kwargs):
    from carpenter.db import get_db
    db = get_db()
    try:
        conditions = []
        bind_vals = []
        if tool_input.get("status"):
            conditions.append("ce.execution_status = ?")
            bind_vals.append(tool_input["status"])
        if tool_input.get("source"):
            conditions.append("cf.source = ?")
            bind_vals.append(tool_input["source"])
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        limit = tool_input.get("limit", 20)
        bind_vals.append(limit)
        rows = db.execute(
            f"SELECT ce.id, cf.file_path, cf.source, cf.arc_id, "
            f"ce.execution_status, ce.exit_code, ce.executor_type, "
            f"ce.started_at, ce.completed_at, ce.log_file "
            f"FROM code_executions ce "
            f"JOIN code_files cf ON ce.code_file_id = cf.id "
            f"{where} ORDER BY ce.id DESC LIMIT ?",
            tuple(bind_vals),
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return "No code executions found."
    lines = []
    for r in rows:
        arc = f" arc=#{r['arc_id']}" if r["arc_id"] else ""
        lines.append(
            f"exec#{r['id']} [{r['execution_status']}] exit={r['exit_code']}{arc}\n"
            f"  file: {r['file_path']}\n"
            f"  source: {r['source']}  executor: {r['executor_type']}"
        )
    return "\n".join(lines)


@chat_tool(
    description=(
        "Read the log output of a specific code execution by its execution ID. "
        "Returns stdout/stderr captured during the run."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "execution_id": {
                "type": "integer",
                "description": "The code_executions ID to read output for.",
            },
        },
        "required": ["execution_id"],
    },
    capabilities=["database_read"],
)
def get_execution_output(tool_input, **kwargs):
    from carpenter.db import get_db
    execution_id = tool_input["execution_id"]
    db = get_db()
    try:
        row = db.execute(
            "SELECT ce.*, cf.file_path, cf.source FROM code_executions ce "
            "JOIN code_files cf ON ce.code_file_id = cf.id "
            "WHERE ce.id = ?",
            (execution_id,),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return f"Execution #{execution_id} not found."
    parts = [
        f"Execution #{row['id']}",
        f"  Status: {row['execution_status']}  Exit code: {row['exit_code']}",
        f"  File: {row['file_path']} (source: {row['source']})",
        f"  Executor: {row['executor_type']}",
    ]
    # Check taint status — fail-closed
    taint_source = None
    try:
        taint_source = row["taint_source"] if row["taint_source"] else None
    except (KeyError, IndexError):
        pass

    if taint_source is None:
        try:
            with open(row["file_path"]) as f:
                code_content = f.read()
            from carpenter.security.trust import check_code_for_taint
            taint_source = check_code_for_taint(code_content)
        except Exception:
            logger.warning(
                "Taint check failed for execution #%s; withholding output (fail-closed)",
                row["id"],
                exc_info=True,
            )
            parts.append(
                "\n  Output withheld: could not verify taint status. "
                "To access this data, create an untrusted arc batch via "
                "arc.create_batch() with REVIEWER and JUDGE arcs. "
                "See kb entry [[web/trust-warning]] for the exact pattern."
            )
            return "\n".join(parts)

    if taint_source:
        output_key = f"exec_{execution_id:06d}"
        parts.append(
            f"\n  Output withheld: code uses untrusted tools ({taint_source}). "
            f"Output key: {output_key}. "
            f"To access this data, create an untrusted arc batch via "
            f"arc.create_batch() with REVIEWER and JUDGE arcs. "
            f"See kb entry [[web/trust-warning]] for the exact pattern."
        )
        return "\n".join(parts)

    log_file = row["log_file"]
    if log_file:
        try:
            with open(log_file) as f:
                output = f.read()
            log_max = config.get_config("arc_log_output_max_length", 8000)
            if len(output) > log_max:
                output = "...(truncated)...\n" + output[-log_max:]
            parts.append(f"\n--- Output ---\n{output}")
        except OSError:
            parts.append(f"\n  Log file not accessible: {log_file}")
    else:
        parts.append("\n  (no log file)")
    return "\n".join(parts)


@chat_tool(
    description=(
        "List recent AI API calls with token usage and cache metrics. Shows "
        "input/output tokens, cache_creation and cache_read tokens, model, and "
        "stop_reason. Use to monitor prompt caching efficiency and API costs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "integer",
                "description": "Filter to a specific conversation. Omit for all.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 20).",
            },
        },
        "required": [],
    },
    capabilities=["database_read"],
)
def list_api_calls(tool_input, **kwargs):
    from carpenter.db import get_db
    db = get_db()
    try:
        conditions = []
        bind_vals = []
        if tool_input.get("conversation_id"):
            conditions.append("conversation_id = ?")
            bind_vals.append(tool_input["conversation_id"])
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        limit = tool_input.get("limit", 20)
        bind_vals.append(limit)
        rows = db.execute(
            f"SELECT id, conversation_id, model, input_tokens, output_tokens, "
            f"cache_creation_input_tokens, cache_read_input_tokens, "
            f"stop_reason, created_at "
            f"FROM api_calls {where} ORDER BY id DESC LIMIT ?",
            tuple(bind_vals),
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return "No API calls recorded."
    lines = []
    for r in rows:
        cache_read = r["cache_read_input_tokens"]
        cache_create = r["cache_creation_input_tokens"]
        total_in = r["input_tokens"]
        hit_rate = (cache_read / (total_in + cache_create + cache_read) * 100) if (total_in + cache_create + cache_read) > 0 else 0
        lines.append(
            f"api#{r['id']} conv={r['conversation_id']} {r['model']} "
            f"stop={r['stop_reason']}  ({r['created_at']})\n"
            f"  in={r['input_tokens']} out={r['output_tokens']} "
            f"cache_create={cache_create} cache_read={cache_read} "
            f"hit_rate={hit_rate:.1f}%"
        )
    return "\n".join(lines)


@chat_tool(
    description=(
        "Get aggregated prompt caching statistics. Shows total tokens, cache "
        "hit rate, estimated cost savings, and per-conversation breakdown. Use "
        "to understand caching effectiveness."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "integer",
                "description": "Limit stats to a specific conversation. Omit for global stats.",
            },
        },
        "required": [],
    },
    capabilities=["database_read"],
)
def get_cache_stats(tool_input, **kwargs):
    from carpenter.db import get_db
    db = get_db()
    try:
        conditions = []
        bind_vals = []
        if tool_input.get("conversation_id"):
            conditions.append("conversation_id = ?")
            bind_vals.append(tool_input["conversation_id"])
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        row = db.execute(
            f"SELECT COUNT(*) as call_count, "
            f"SUM(input_tokens) as total_input, "
            f"SUM(output_tokens) as total_output, "
            f"SUM(cache_creation_input_tokens) as total_cache_create, "
            f"SUM(cache_read_input_tokens) as total_cache_read "
            f"FROM api_calls {where}",
            tuple(bind_vals),
        ).fetchone()

        breakdown_sql = (
            f"SELECT conversation_id, COUNT(*) as calls, "
            f"SUM(input_tokens) as input_sum, "
            f"SUM(cache_read_input_tokens) as cache_read_sum, "
            f"SUM(cache_creation_input_tokens) as cache_create_sum "
            f"FROM api_calls {where} GROUP BY conversation_id "
            f"ORDER BY conversation_id DESC LIMIT 5"
        )
        conv_rows = db.execute(breakdown_sql, tuple(bind_vals)).fetchall()
    finally:
        db.close()

    if not row or row["call_count"] == 0:
        return "No API calls recorded yet."

    total_input = row["total_input"] or 0
    total_output = row["total_output"] or 0
    total_cache_create = row["total_cache_create"] or 0
    total_cache_read = row["total_cache_read"] or 0
    call_count = row["call_count"]

    full_price_equivalent = total_input + total_cache_create + total_cache_read
    actual_cost_units = total_input + (total_cache_create * 1.25) + (total_cache_read * 0.1)
    savings_pct = ((full_price_equivalent - actual_cost_units) / full_price_equivalent * 100) if full_price_equivalent > 0 else 0

    overall_hit_rate = (total_cache_read / full_price_equivalent * 100) if full_price_equivalent > 0 else 0

    parts = [
        f"API Call Statistics ({call_count} calls)",
        f"  Total input tokens:    {total_input:,}",
        f"  Total output tokens:   {total_output:,}",
        f"  Cache creation tokens: {total_cache_create:,}",
        f"  Cache read tokens:     {total_cache_read:,}",
        f"",
        f"  Cache hit rate:        {overall_hit_rate:.1f}% of input from cache",
        f"  Estimated savings:     {savings_pct:.1f}% vs no caching",
        f"  (Cache reads are 10% of input price, creation is 125%)",
    ]

    if conv_rows:
        parts.append(f"\nPer-conversation breakdown (recent {len(conv_rows)}):")
        for cr in conv_rows:
            conv_total = (cr["input_sum"] or 0) + (cr["cache_create_sum"] or 0) + (cr["cache_read_sum"] or 0)
            conv_hit = ((cr["cache_read_sum"] or 0) / conv_total * 100) if conv_total > 0 else 0
            parts.append(
                f"  conv#{cr['conversation_id']}: {cr['calls']} calls, "
                f"cache_read={cr['cache_read_sum'] or 0:,}, "
                f"cache_create={cr['cache_create_sum'] or 0:,}, "
                f"hit_rate={conv_hit:.1f}%"
            )

    return "\n".join(parts)
