**Execute Arc #{{ arc_id }}**

{{ goal }}

Use `submit_code` to accomplish this task. Submit code in a SINGLE call — do not use read tools or explore first, just submit the code.

The conversation_id ({{ source_conv_id }}) and arc_id ({{ arc_id }}) are auto-injected into the execution environment — you do NOT need to pass them explicitly.

To send a chat message, submit this exact pattern:
```python
from carpenter_tools.act import messaging
messaging.send(message="Your message content here")
```

Do NOT explore or read files. Just write and submit the code immediately.