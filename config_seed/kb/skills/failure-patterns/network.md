# Network Failures

Failures related to HTTP communication between platform components and external services.

---

## Callback Timeout

**Symptoms**: Executed code hangs or times out while making a callback to the platform. The `carpenter_tools` operation (e.g., `set_state()`, `send_message()`, `create_arc()`) does not return within a reasonable time. The execution may eventually be killed by the overall execution timeout.

**Root cause**: The platform's HTTP server is busy or blocked. Possible causes:
- The main event loop is processing a long-running work item.
- The SQLite database is locked by another write operation (see `state.md` for DB locked).
- The server is under heavy callback load from concurrent executions.
- The FastAPI server's thread pool is exhausted.

**Escape**:
1. Retry the execution. Transient server load often resolves on its own.
2. If callbacks consistently fail, check platform server logs for errors or warnings.
3. Reduce the number of callbacks per execution. Batch state updates into fewer calls.
4. If the code makes many rapid sequential callbacks, add small delays between them (e.g., `time.sleep(0.1)` between state.set calls).
5. For read-only operations, use direct Python file I/O or `carpenter_tools.read.*` instead of callbacks -- these are lower overhead.

**Prevention**: Minimize callback frequency. Batch operations where possible. Use read-only tools (which bypass the callback mechanism) when write access is not needed.

**Escalation**: If callback timeouts are chronic, the platform may need performance tuning. Inform the user.

---

## DNS Resolution Failure

**Symptoms**: Execution log shows `socket.gaierror: [Errno -2] Name or service not known` or similar DNS resolution errors when the code makes external HTTP requests.

**Root cause**: The system cannot resolve the target hostname to an IP address. Possible causes:
- The system has no internet connectivity.
- The DNS server is unreachable or misconfigured.
- The hostname is misspelled or does not exist.
- A local DNS cache has stale entries.

**Escape**:
1. Verify the hostname is correct (check for typos).
2. Check basic connectivity: the code could try a known-good endpoint first (e.g., `httpx.get("https://httpbin.org/status/200", timeout=5)`) to distinguish DNS failure from target-specific issues.
3. If the system has no internet access (e.g., air-gapped deployment), inform the user. External network requests are not possible in this environment.
4. If DNS is intermittent, retry with backoff.

**Prevention**: Always validate URLs before making requests. Include timeouts on all network calls. Consider whether the operation truly requires external network access.

**Escalation**: If the deployment has no internet access by design, inform the user that web-fetching operations cannot work. Suggest alternative approaches (local files, pre-cached data).

---

## Rate Limiting (AI Provider)

**Symptoms**: API calls to the AI provider return HTTP 429 (Too Many Requests). Error messages may include "rate_limit_error", "Rate limit exceeded", or "Too many requests". The platform's built-in rate limiter may log warnings about approaching limits.

**Root cause**: The AI provider enforces rate limits on API calls (requests per minute, tokens per minute). The platform has its own rate limiter (`rate_limit_rpm`, default 45; `rate_limit_itpm`, default 35000) but provider-side limits may be stricter depending on the API tier.

**Escape**:
1. The platform's `_call_with_retries()` in invocation.py handles 429 responses with automatic backoff and retry (up to `mechanical_retry_max` attempts, default 4).
2. If retries are exhausted, wait and try again. Rate limits typically reset within 60 seconds.
3. Check if multiple conversations or arcs are making concurrent API calls -- total load across all operations counts against the same rate limit.
4. If using Anthropic: check the API tier. Higher tiers have higher rate limits.
5. If the rate limit is on the reviewer model specifically, consider using a different model for review that has a separate rate limit pool.

**Prevention**: The platform rate limiter is configured conservatively by default. Avoid rapid sequential tool iterations that each trigger an API call. Batch operations where possible.

**Escalation**: If rate limiting is a persistent problem, the user may need to upgrade their API tier or reduce concurrent operations.

---

## Rate Limiting (External APIs)

**Symptoms**: Submitted code that fetches external URLs receives HTTP 429 responses from the target website or API. This is separate from AI provider rate limiting.

**Root cause**: The target website or API has its own rate limits. Web scraping, rapid API calls, or repeated requests to the same endpoint trigger these limits.

**Escape**:
1. Add delays between requests (e.g., `time.sleep(1)` between each URL fetch).
2. Respect `Retry-After` headers if present in the 429 response.
3. If scraping multiple pages, implement exponential backoff.
4. Check if the target API offers a bulk endpoint that can fetch multiple items in one request.
5. Consider caching results in platform state to avoid re-fetching the same data.

**Prevention**: Always add delays between requests to external services. Prefer APIs with explicit rate limit documentation and stay well within their limits.

**Escalation**: If the target API's rate limits are too restrictive for the task, inform the user. They may need to obtain higher-tier API access or use a different data source.

---

## Connection Refused

**Symptoms**: Execution log shows `ConnectionRefusedError: [Errno 111] Connection refused` or `httpx.ConnectError`. The target host is reachable at the network level but the specific port is not accepting connections.

**Root cause**: The service at the target address is not running, not listening on the expected port, or is temporarily unavailable. For platform callbacks, this usually means the platform server has stopped or restarted.

**Escape**:
1. For callback connection refused: the platform server is likely not running. Check server status.
2. For external service connection refused: verify the URL and port are correct. The service may be down.
3. For Ollama connection refused (at `ollama_url`): the Ollama server is not running. The user needs to start it (`ollama serve`).
4. For Forgejo connection refused: the Forgejo instance at `git_server_url` is not reachable. Check the URL and server status.

**Prevention**: For critical operations, implement retry logic with backoff. For optional operations (e.g., fetching supplementary data), handle connection errors gracefully and continue without the data.

**Escalation**: If a required service is down, inform the user. Operations that depend on that service cannot proceed until it is restored.

---

## SSL/TLS Certificate Error

**Symptoms**: Execution log shows `ssl.SSLCertVerificationError` or `httpx.ConnectError` with certificate-related messages. HTTPS connections fail.

**Root cause**: The target server's SSL certificate is invalid, expired, self-signed, or the system's CA certificate bundle is outdated. Common in development environments with self-signed certificates.

**Escape**:
1. Do NOT disable SSL verification (`verify=False`) as a workaround in submitted code -- this will likely be flagged by the review pipeline as a security concern.
2. If the target is a local service with a self-signed certificate, inform the user. They may need to add the certificate to the system's trust store.
3. If the system's CA bundle is outdated, the user needs to update it (`pip install certifi` and/or update system certificates).

**Prevention**: Use HTTPS endpoints with valid certificates. For internal services, ensure proper certificate management.

**Escalation**: Certificate management is a system administration task. Inform the user of the specific certificate error and let them handle the resolution.
