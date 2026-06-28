---
description: Wrapper prompt for the reflect step — overrides the default submit_code arc-step wrapper so the EXECUTOR replies directly with the requested JSON.
---
**Reflect step (Arc #{{ arc_id }})**

{{ goal }}

## Response mode

Reply directly with the JSON object specified in the goal above. Do
**not** call `submit_code`, do not wrap the JSON in a Markdown fence,
and do not add prose before or after it. The platform parses your
reply as raw JSON and validates it against the `ReflectionResult`
schema; anything other than a single JSON object is treated as an
empty reflection.
