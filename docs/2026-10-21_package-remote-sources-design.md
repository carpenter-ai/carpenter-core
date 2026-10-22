# Design: remote capability-package sources and chat-requested installs

Status: implemented. Reference documentation is
[`docs/package-sources.md`](package-sources.md); this document records the *decisions* and
why they were taken, because the code cites them by number (`D1`…`D13`).

**Target user story.** A fresh Carpenter is asked in chat to install two capability
packages that live in two different remote git repositories (or at an archive URL), and
after the install the operator can use them — without a terminal, and without any of the
fetched code having run before a human agreed to it.

Before this work, none of that was possible. `install_package` is operator-only and is
stripped from the chat agent's tool set whenever no confirmation handler is registered,
which is the case on every platform package; the only install path was a local directory
named on the CLI. A package declaring `platform_capabilities` could not be installed from
chat at all, because the chat tool passes no `capability_input_fn` and the trust-ack
refuses to grant non-interactively. So the work had three legs, not one: remote sources, a
path from chat to an authorised install, and provenance plus upgrade.

## What already existed, and was not rebuilt

- `installer.install_package(source_path, dest_path, ...)` installs from **any** local
  directory, and the hash is computed over the materialised tree — so fetch-to-quarantine
  then install yields the same hash as a local install.
- `archive_cache`'s hardened tar extractor (`_safe_extract` / `_expand_to_tree` /
  `_verify_archive_tree`). Reused, never re-implemented.
- `archive_cache.ArchiveFetcher` plus `load_pristine_tree(..., expected_root_hash)` — a
  *verify-against-known-hash* seam for reconcile. It does not fit a first install, where no
  prior hash exists, so a second seam was added rather than bending this one.
- `reconcile.classify` and `reconcile_apply.apply_reconciled_install`: a complete, tested
  three-way merge engine that was wired to nothing.
- dulwich and httpx were already dependencies, and nothing in the repository shells out to
  the `git` binary.
- `security/egress.py`'s `check_url()` and the `web_egress_allowlist` config key — the
  platform's one network allowlist.

## Decisions

**D1 — The trust anchor for a first install is operator consent on displayed
provenance**: host, redacted URL, requested ref, resolved commit or digest, tree hash and
manifest summary, plus a reachability gate. Not signatures. The single verification point
stays open: `fetch_source` is the one place a signature or attestation check slots in.

**D2 — Reachability reuses `web_egress_allowlist`.** A package fetch calls
`egress.check_url()` like any other outbound request; no third allowlist concept was
invented. A fresh install of a package from a non-routable host therefore has an explicit
operator step, and the refusal names the exact config key and host. A package's own
`allowlist_proposals` touch only JUDGE value allowlists and must never feed egress.

**D3 — A fetch never executes fetched code.** Bytes land in a `0700` quarantine under
`{base_dir}/cache/package-staging/`, which is on no registry search path, under byte, file,
per-file and wall-clock caps. Archives go through the existing hardened extractor; a git
tree gets the same member rules (no symlink escapes, no absolute paths, no submodules, no
`.git` in the installed tree). The pre-consent preview is built **without importing
package python**: `classify_package_chat_tools` imports the source tree to read decorator
metadata, which for a remote source is arbitrary code execution before the trust prompt
that was meant to inform the decision, so remote previews use an AST inspection instead
(`static_preview.py`).

**D4 — Provenance is a first-class column set, and source identity is not the package
name.** `source_kind`, `source_url` (redacted), `source_ref`, `source_resolved`,
`source_subdir`, `source_id` and `fetched_at` on `installed_packages`, where `source_id`
hashes the logical source so two remotes publishing the same package name cannot be
confused. The archive cache keyed on `(name, version)` with no source discriminator; that
was fixed additively, and the old layout is still read.

**D5 — A multi-package install is one consent and a compensating rollback.** No database
surgery for cross-package atomicity: fetch all, preview all, take one operator decision for
the set, install each in its own transaction, and on any failure uninstall the ones *this
batch* installed — never one it merely upgraded — then report exactly what happened per
package. "Install X and Y" must never leave a silent half-state.

**D6 — Upgrade wires the reconcile engine** rather than adding a second merge.
`update_package(name)` re-fetches the recorded source at its ref, three-way reconciles the
pristine tree of the installed version, the live tree and the pristine tree of the new
version, and applies the result. Where the pristine anchor is missing it degrades to an
explicit clobber plan that lists what it will overwrite, rather than pretending to merge.

**D7 — A first install can hot-load; a re-install cannot.** The module cache only bites a
package already imported in this process, so a newly installed package can be registered
live by the same registry calls startup makes — which is what makes "install X and Y, then
use them" work in one chat session. A package already loaded is refused with
`needs_restart=True`, because re-registering it would run the *old* module bodies while
the operator believed the new ones were live.

**D8 — Credentials come from the platform, never from a package.** A configured source
names an environment variable (`credential_ref`); the value is injected at fetch time, sent
to one origin, never persisted, never logged, and redacted in previews and in the database.

**D9 — No forge-specific fetch code.** A Gitea-compatible release asset is just an archive
URL and a monorepo is `git+https://…#subdir=packages/<name>`, so there are exactly two
fetchers and no provider adapters on this path.

**D10 — No python dependency installation.** Packages import the platform's dependencies
and the standard library only.

**D11 — Chat reaches install through a two-phase request and approval, not a blocking
confirmation handler.** The platform's confirmation seam is `(tool_name, tool_input) ->
bool`: synchronous, with no timeout and no conversation id, and registering a handler is
also what makes operator-only tools visible at all. The turn runs on a worker thread, so
blocking is *mechanically* possible — but an operator's "yes" typed in chat while a turn is
pending is persisted and then dropped, because `deliver_inbound` starts no task while a
turn is in flight. A blocking handler is therefore the wrong shape.

So: a new chat tool `request_package_install(sources)`, available to the chat agent and
refused to arcs, resolves and fetches into quarantine, builds the preview statically,
writes a `pending_package_installs` row and returns a one-time approval URL. The operator
approves there, where the requested `platform_capabilities` are listed and granted
explicitly per package — which is what finally makes a capability-declaring package
installable without a terminal. On approval the platform performs the install itself, out
of the model turn, then hot-loads or says a restart is needed. `install_package` stays
operator-only and nothing about its enforcement changes.

The precedent is the arc-approval review link: a one-time UUID URL, an approve/reject page,
and state that survives a restart. Two deliberate differences:

- The row lives in **SQL**, not in a process-global dict as `review.py` keeps its links. A
  package install has no arc to hang durability off, so SQL makes restart survival free and
  needs no recovery function for the row itself.
- **Deciding requires platform authentication**; reading the page does not. See D11.3.

**D11.1 — The approval route lives in carpenter-core.** The platform packages ship no HTTP
server; the server is core's. So `carpenter/api/package_install.py` is a parallel module to
`api/review.py`, reusing its URL absolutisation, its static templating and its
uuid4/one-shot/`404`/`410` contract.

**D11.2 — A new chat tool must be a new seed module.** `install_chat_tool_defaults` copies
a seed module only when the destination does not exist; an edited one is reported as
drifted and left alone. A *new* seed file therefore reaches an existing install on its next
start, while a change to an existing seed file benefits fresh installs only and must be
applied by hand on an existing one.

**D11.3 — Approving requires authentication, and the quarantine is never re-fetched.**
Two amendments made during implementation, the first after an adversarial review:

- The approval URL is returned to the chat agent that asked and is persisted in the
  conversation, and the platform ships an HTTP POST dispatch verb — so the model that
  requested the install held the credential that would approve it, and a prompt-injected
  agent could request and approve in one move. `GET` stays unauthenticated so a human can
  open the link from a chat app or a mail client; `POST .../decide` now requires a session
  authenticated to the platform in addition to the single-use token. This is a deliberate
  departure from the review-link precedent, justified by what is being authorised:
  approving an install runs unsandboxed code in the platform process, which is not
  comparable to approving a reviewed arc step.
- The fetched tree is **kept** in its quarantine and re-hashed against the previewed hash
  at install time, never re-fetched. Re-fetching at approval time is a TOCTOU hole: the
  operator approves tree hash H and a hostile remote serves H′ to the second fetch.

**D12 — End-to-end verification does not depend on a model.** Every test in the suite is
hermetic: temp directories, local bare git repositories served over loopback, and a
loopback HTTP server for archives. Nothing touches the network. The end-to-end rehearsal
against a real remote drives the platform's own APIs and writes the tool call into the
dispatch path rather than asking a model to produce it, so the whole path — request,
approval, work-queue install, hot-load, rollback, upgrade — is provable without a provider
call. The model-driven acceptance stories in the platform package cover the same ground
from the operator's side, at the cost of real provider calls.

**D13 — A platform package already shipped a concrete remote fetcher**
(`carpenter_linux/packages/github_fetcher.py`) implementing the `ArchiveFetcher` protocol
for the pristine/upgrade path. The archive fetcher generalises it rather than duplicating
it; refactoring that implementation onto the new seam is a follow-up, not part of this
change.

## Code layout

| Module | Role |
|---|---|
| `carpenter/packages/sources.py` | Spec grammar, canonicalisation, `source_id`, redaction, the configured-source registry |
| `carpenter/packages/fetch.py` | `FetchLimits` / `FetchResult` / `Fetcher`, `fetch_source`, `cleanup`, the egress gate, the local fetcher |
| `carpenter/packages/fetch_git.py` | dulwich clone at a ref, subtree materialisation, tree hardening |
| `carpenter/packages/fetch_archive.py` | Streamed download, sha256 pinning, lazy tar-index capping, hardened extraction |
| `carpenter/packages/static_preview.py` | The AST-only chat-tool preview (D3) |
| `carpenter/packages/install_requests.py` | Resolve → fetch → preview → row → one-time URL (D11) |
| `carpenter/packages/pending_install.py` | The request row, its DDL and its state machine |
| `carpenter/api/package_install.py` | The approval page and the decide route (D11.1, D11.3) |
| `carpenter/packages/install_approved_handler.py` | The `package.install.approved` work handler |
| `carpenter/packages/install_batch.py` | Per-package transactions, hash re-verification, compensating rollback (D5) |
| `carpenter/packages/install_notify.py` | The zero-model outcome message |
| `carpenter/packages/reload.py` | Live load, and when it refuses (D7) |
| `carpenter/packages/update.py` | Re-fetch at the recorded ref, three-way reconcile (D6) |
| `carpenter/packages/discover.py` | Listing a configured source, metadata only |

An earlier sketch had a single `orchestrate.py` owning resolve → fetch → preview → consent
→ install. It split in two along the consent boundary, because everything before consent
runs inside a model turn and everything after runs on the work queue, and the two halves
share no state but the row.
