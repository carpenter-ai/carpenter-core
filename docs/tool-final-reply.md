# Tool-Supplied Final Replies

## Overview

Normally a chat tool's result goes back to the model, and the model writes
the reply the user sees. For some tools that is the wrong shape: the
result *is* the reply, and any rewording is a risk. An asynchronous
"order submitted" tool is the typical case. Its acknowledgement says only
that the order was accepted, but a model paraphrasing it can invent an
outcome ("your order has been delivered") that has not happened.

A tool opts out of that rewrite, per call, by returning `FinalReply`.

```python
from carpenter.chat_tool_loader import FinalReply, chat_tool

@chat_tool(
    description="Submit an order. Confirmation arrives later.",
    input_schema={...},
)
def submit_order(tool_input, **kwargs):
    try:
        order_id = queue_order(tool_input)
    except BackendError as e:
        return f"Error: could not submit the order: {e}"  # model explains
    return FinalReply(
        f"Order {order_id} submitted. You will get a message when it "
        f"is confirmed."
    )
```

It works the same for every kind of chat tool: `config/chat_tools/`
modules, capability-package tools, `register_extension_tool()` and
`register_tool_handler()` handlers. No decorator flag or registration
option is involved, so the package registry's rule that packages cannot
set platform-side tool flags is unaffected.

## Behaviour

When a tool returns a non-empty `FinalReply`, the invocation loop
(`invoke_for_chat` in `carpenter/agent/invocation.py`):

1. Stores the text as that tool's `tool_result`, like any other result,
   so the model sees it in the conversation history on later turns.
2. Persists the text, verbatim, as an `assistant` message.
3. Ends the turn. No further model call is made for it.

The stored history stays well-formed: the `tool_use` turn, its
`tool_result` turn and the reply follow each other as they would after
any normal tool loop.

`invoke_for_chat` returns the reply's message id as `message_id`.
Channel connectors send the latest assistant message
(`ChannelConnector._run_and_respond`), which is this reply, so the
channel delivers exactly that text, once.

### Chat turns only

`invoke_for_chat` also runs arc steps (an arc's agent working through
its step, with `_executor_arc_id` set). There, ending after one batch
of tool calls would cut the step short, so `FinalReply` is treated as a
plain result: the text goes back to the model as the tool result and
the step continues as usual.

### Errors

The choice is made per call, by the return type. If the tool returns a
plain string, raises, is declined at a confirmation prompt, or fails input
validation, the result is an ordinary one and the model is called again
as usual, so it can explain the failure. Return `FinalReply` only on
success.

An empty or whitespace-only `FinalReply` is treated as a plain result.

### Several tool calls in one response

Every tool call in the batch is executed and gets its `tool_result`, as
usual. If at least one returned `FinalReply`, the turn ends after the
batch:

- Several `FinalReply` texts are joined with a blank line, in the order
  the model called the tools.
- Plain results from other tools in the batch are recorded in the
  history but get no commentary in this turn. The model sees them on
  the next turn.

### Text the model wrote alongside the call

If the model wrote visible text in the same response as the tool call
(for example "Placing the order now."), that text is kept as its own
assistant message before the reply, as it always is. Interfaces that show
the whole conversation show both. Channel connectors send only the latest
assistant message, which is the tool's reply.

### Long replies

The reply is captured before tool-output truncation, so the user gets
the full text even when the copy kept for the model's context is
truncated.

## Notes for tool authors

- The text reaches the user without passing through the model, so the
  tool is responsible for it. Keep it factual and say only what the tool
  knows. Do not build it from untrusted content you would not show the
  user verbatim.
- Results of the tool are still tool results: the model reads them on
  later turns, under the same rules as any other tool output.
- `FinalReply` is a `str` subclass. Code that handles results as strings
  keeps working, and returning a plain `str` keeps the old behaviour.
