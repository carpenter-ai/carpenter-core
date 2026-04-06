# Conversation Management

## Chat tools (read-only)

These are available as direct chat tools:

- `list_conversations(include_archived?, archived_only?, limit?)` — List conversations with preview of first message
- `get_conversation_messages(conversation_id)` — Get all messages in a conversation

These are read-only. **Chat tools cannot modify conversations** (trust invariant I10).

## Python module tools (via submit_code ONLY)

Write operations require `submit_code`. Import from `carpenter_tools.act.conversation`:

- `conversation.rename(conversation_id, title)` — Set conversation title
- `conversation.archive(conversation_id)` — Archive a single conversation
- `conversation.archive_batch(conversation_ids)` — Archive multiple conversations at once
  - `conversation_ids`: list of int IDs to archive
  - Returns `{"archived_count": N, "conversation_ids": [...]}`
- `conversation.archive_all(exclude_ids=None)` — Archive all conversations
  - `exclude_ids`: optional list of int IDs to keep unarchived
  - Returns `{"archived_count": N}`

### Examples

Archive specific conversations:
```python
conversation.archive_batch([1, 2, 3, 15, 22])
```

Archive everything except the current conversation:
```python
conversation.archive_all(exclude_ids=[current_conversation_id])
```

## Other submit_code tools

- `credentials.request(key, label?, description?)` — Create secure one-time link for credential input
- `credentials.verify(key)` — Test a stored credential
- `credentials.import_file(path, key)` — Import credential from file

## Related
[[chat/utilities]]
