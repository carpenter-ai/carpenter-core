# KB Editing

Create, edit, and delete Knowledge Base entries.

## Action tools (in submitted code)
```python
from carpenter_tools.act import kb
kb.add(path="topic/new-entry", content="# Title\n\nContent...", description="Short description")
kb.edit(path="topic/existing", content="# Updated\n\nNew content...")
kb.delete(path="topic/obsolete")
```

## Rules
- All KB writes go through `submit_code` review pipeline
- Cannot delete auto-generated entries
- Cannot overwrite `<!-- auto:... -->` sections
- Target size: 300-800 bytes per entry (soft cap: 6000 bytes)
- Use `[[wiki-links]]` to connect related entries

## Entry format
```markdown
# Title

One-line description for indexes and search.

## Content
Details, tool references, code examples...

## Related
[[other/entry]] · [[another/entry]]
```

## Health checks
Use `get_kb_health` to check knowledge base health. Report broken links, orphan entries, or oversized entries. Suggest fixes using `kb.edit()` or `kb.add()`.

## Related
[[kb/navigation]] · [[security/review-pipeline]]
