# Credential Intake for External Services

When a user provides an external git repository URL (e.g. a Forgejo, Gitea, or GitHub URL), handle credentials BEFORE creating any workflow arcs.

## Required flow
1. Recognize the URL is an external repository that needs API access
2. Use `verify_credential` with key `FORGEJO_TOKEN` to check if a token is already configured and valid
3. If NOT valid, use `request_credential` to create a secure one-time link:
   - key: `FORGEJO_TOKEN`
   - label: a human-readable name (e.g. 'Forgejo API Token')
   - description: explain what the token is for
4. Present the credential link URL to the user and ask them to provide their token via that link
5. When the user confirms, use `verify_credential` to confirm it works
6. Only AFTER verification succeeds, proceed with the workflow setup

Do NOT skip credential intake and jump straight to creating arcs — the workflow will fail without valid API credentials.

The credential is stored securely in `.env` and never appears in chat.

## Related
[[git/tools]] · [[arcs/planning]]
