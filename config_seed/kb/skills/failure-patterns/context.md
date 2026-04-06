# Context and Conversation Failures

Failures related to the chat agent's context window, conversation management, and taint-based restrictions.

---

## Context Window Exhaustion

**Symptoms**: The AI provider returns an error about the input being too long, the request exceeding the context window, or max tokens being exceeded. Alternatively, the model's responses become increasingly degraded, repetitive, or incoherent as the conversation grows.

**Root cause**: Every message in the conversation -- user messages, assistant responses, tool calls, and tool results -- consumes context window tokens. Long conversations accumulate context until the model's limit is reached. The platform does not yet implement active context compaction (the compaction logic in invocation.py is a placeholder).

**Escape**:
1. If the error is explicit (API error about context length): the conversation must be shortened.
2. Start a new conversation. The conversation boundary memory system will generate a summary of the current conversation, and the new conversation will receive prior context from that summary.
3. To manually trigger the boundary: simply begin a new conversation via the chat interface. After a 6-hour gap (configurable via `context_compaction_hours`), a summary is automatically generated.
4. If the conversation is long because of many tool call results: large tool outputs are the primary cause of context bloat. The platform truncates outputs over `tool_output_max_bytes` (default 32 KB), but many smaller outputs still accumulate.
5. For active conversations that cannot be restarted: reduce tool usage. Give concise instructions that require fewer iterations. Avoid requesting exploratory tool use (e.g., "read all files in this directory") when you can be more specific.

**Prevention**: Keep conversations focused on a single topic or task. Start new conversations for new topics. The platform's conversation boundary memory ensures context carries over across conversation boundaries.

**Escalation**: Context compaction is not yet fully implemented. For long-running tasks that genuinely need long context, inform the user that this is a current platform limitation.

---

## Tool Output Too Large

**Symptoms**: A tool call returns a truncated result with a notice like "[truncated: showing first N / last M of K lines (B bytes). Full output saved to: /path/to/file]". The agent does not see the complete output.

**Root cause**: The platform's `_maybe_truncate_tool_output()` function in invocation.py truncates tool results that exceed `tool_output_max_bytes` (default 32768 bytes = 32 KB). The full output is saved to disk, but only the head and tail are included in the context window.

**Escape**:
1. Read the truncation notice carefully. It tells you where the full output was saved.
2. If you need specific information from the full output, use `read_file` to read the saved file, specifying offset and length to target the relevant section.
3. Redesign the approach to avoid generating large outputs:
   - Instead of reading an entire large file, read specific line ranges.
   - Instead of listing all files in a large directory, use more specific paths.
   - Instead of fetching a complete web page, extract just the needed information.
4. For `list_files` on large directories: use more specific subdirectory paths or filter criteria.
5. For `read_file` on large files: use offset/length parameters to read chunks.

**Prevention**: Be precise in tool calls. Request only the data you need. For `read_file`, specify line ranges for large files. For `list_files`, target specific subdirectories. For code execution, have the code produce concise output.

**Escalation**: The truncation limit can be increased via `tool_output_max_bytes` in the configuration, but this trades context window space for completeness. Inform the user if the default limit is consistently too restrictive for their workflow.

---

## Conversation Taint Preventing Skill KB Modification

**Symptoms**: A skill KB modification (via `kb.add` or `kb.edit` to a `skills/` path) triggers human-escalation review. The modification is blocked pending user approval.

**Root cause**: When submitted code imports or uses untrusted tool modules (e.g., `carpenter_tools.act.web`), the conversation is marked as tainted in the `conversation_taint` table. The taint check uses a fail-closed approach: if the taint check itself fails, the conversation is treated as tainted. The `skill-kb-review` template workflow detects tainted sources and requires human approval before the KB modification takes effect.

**Escape**:
1. Check whether the conversation is actually tainted: query `conversation_taint` table for the conversation ID.
2. If the conversation was tainted by a web fetch or other untrusted tool: the restriction is by design. You have two options:
   a. Inform the user that the skill KB modification requires their review and approval. The platform will notify them.
   b. Start a new conversation (which will be clean) and make the KB modification there.
3. If the conversation was tainted by a taint-check failure (fail-closed): this is a false positive. The conversation is treated as tainted out of caution. A new conversation will be clean.
4. The skill-kb-review pipeline runs automatically when writing to `skills/` KB paths. Even for clean conversations, an AI intent review is performed (but human escalation is skipped when the source is clean and the review passes).

**Prevention**: If you plan to modify skill knowledge, do the KB modification before fetching any untrusted data. Once a conversation is tainted, it cannot be un-tainted. Alternatively, use separate conversations for web research vs. skill maintenance.

**Escalation**: Taint-based restrictions are a security feature. If the user needs to create skill KB entries based on web research, they should review and approve the content when prompted by the human-escalation step.

---

## Summary Generation Failure

**Symptoms**: No conversation summary is generated when starting a new conversation after a gap. The `summary` column in the `conversations` table is NULL for the previous conversation. Memory recall tools return no results for past conversations.

**Root cause**: Summary generation runs in a background thread when a conversation boundary is detected (6-hour gap). It calls the configured summary model (or auto-selects haiku). If the model is unavailable, the API key is missing, or the background thread fails for any reason, the summary is silently not generated.

**Escape**:
1. Check the platform logs for errors during summary generation (look for errors from `conversation.py:generate_summary`).
2. Verify the summary model is configured and reachable: `config.CONFIG.get("summary_model")`. If empty, the platform auto-selects a cheap model.
3. If the API key is missing or expired, summaries cannot be generated.
4. Without summaries, conversation boundary memory degrades gracefully: the new conversation still gets prior context from the last 10 messages of the previous conversation (raw tail), but the structured summary is missing.
5. To manually generate a missing summary: this would require calling `generate_summary()` directly, which is not exposed as a tool. Inform the user of the gap.

**Prevention**: Ensure the summary model is configured and API credentials are valid. Summary generation is lightweight (uses the cheapest available model) but still requires API access.

**Escalation**: If summary generation consistently fails, the conversation memory system is degraded. Inform the user to check API credentials and model configuration.

---

## Chat Tool Iteration Cap

**Symptoms**: The agent's response ends abruptly after several tool calls without completing the task. The response includes partial results but the agent appears to have stopped mid-work.

**Root cause**: The chat tool_use loop is capped at `chat_tool_iterations` (default 10 iterations). Each iteration includes one API call that may invoke one or more tools. Complex tasks that require many sequential tool calls (e.g., reading many files, making many state changes) can exhaust this budget.

**Escape**:
1. If the task is incomplete after hitting the cap, the agent should report what was accomplished and what remains.
2. The user can continue the conversation to trigger additional iterations.
3. For tasks that inherently require many tool calls: restructure the approach to use fewer iterations. For example:
   - Instead of reading 20 files one by one (20 iterations), submit code that reads them all and returns a summary (1 iteration for submit_code).
   - Instead of making many individual state updates, submit code that does all updates in one execution.
4. The `submit_code` tool is the most efficient way to do multi-step work, because the entire code runs in a single iteration regardless of how many operations it performs.

**Prevention**: Favor `submit_code` for complex multi-step operations rather than using many individual tool calls. Each `submit_code` call counts as one iteration but can perform unlimited operations.

**Escalation**: The iteration cap can be increased via `chat_tool_iterations` in the configuration. Inform the user if their typical workflows consistently hit the cap.

---

## KB Search Returns No Results for Past Conversations

**Symptoms**: `kb_search(query, path_prefix="conversations/")` returns no matching conversations even though relevant conversations have occurred in the past.

**Root cause**: KB search queries conversation summary entries by keyword. It will not find conversations that:
- Have not yet been summarized (current conversation, or conversations where summary generation failed).
- Do not contain the search keywords in their title or summary text.
- Were in a different database (test databases are isolated from production).

**Escape**:
1. Try broader search terms. The search is keyword-based, not semantic.
2. Check if summaries exist: conversations without summaries will not appear in keyword searches.
3. Use `kb_describe("conversations/<id>")` if you know the specific conversation path.
4. Browse recent conversation titles (shown in system prompt memory hints) to identify the right conversation.
5. If searching for a topic discussed within a conversation but not in the title/summary, the search will not find it. The full message history is not searched -- only titles and summaries.

**Prevention**: When closing conversations, ensure they get descriptive titles. The title generation happens automatically but requires the title model to be available.

**Escalation**: Full-text search across message content is not currently supported. If the user needs to find specific past discussions, they may need to browse conversations manually.
