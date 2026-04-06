# Config Tools

Runtime configuration changes without a server restart.

## Read tools (`carpenter_tools.read.config`)
```python
from carpenter_tools.read import config
config.get_value(key="memory_recent_hints")   # read a single config value
config.list_keys()                            # all mutable keys with descriptions
config.models()                               # model manifest with capabilities
```

## Action tools (`carpenter_tools.act.config`)
```python
from carpenter_tools.act import config
config.set_value(key="memory_recent_hints", value=5)  # write + hot-reload
config.reload()                                        # reload config.yaml from disk
```

## Notes
- Only keys in the server-side mutable-key allowlist can be changed
- Security-critical settings (API keys, sandbox config) are excluded
- Changes are persisted to `~/carpenter/config.yaml` and hot-reloaded

## Related
[[self-modification/coding-change]] · [[config/models]]
