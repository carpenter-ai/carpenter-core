# PR Review Workflow

Automated PR review triggered by webhook events.

## How it works
1. A webhook subscription listens for `pull_request` events
2. On event, an arc is created using the PR review template
3. The arc fetches the PR diff, analyzes it, and posts a review comment

## Setting up
Use [[git/webhooks]] to subscribe, with `action_type: "create_arc"` and the PR review template.

## Related
[[git/tools]] · [[git/webhooks]] · [[arcs/templates]]
