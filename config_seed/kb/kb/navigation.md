# KB Navigation

How to use the Knowledge Base.

## Tools
- `kb_describe(path?)` — Navigate entries. No path = root index. Folder path = list children. Leaf path = full content.
- `kb_search(query, max_results?)` — Search by keyword or phrase. Returns most relevant entries.
- `kb_links_in(path)` — Find entries that link TO a given path.

## Links
Entries contain wiki-style links (`\[[path]]` and `\[[path|text]]`). Call `kb_describe(path)` to follow any link.

## Entry structure
```
# Title
One-line description.
## Content
Details...
## Related
[[other/entry]] · [[another/entry]]
```

## Tips
- Start at the root and drill down by topic
- Use search when you don't know where to look
- Small entries + many links > large monolithic docs

## Related
[[self-modification/kb-editing]] · [[chat/utilities]]
