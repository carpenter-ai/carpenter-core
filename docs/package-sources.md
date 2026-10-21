# Installing capability packages from a remote source

[`docs/capability-packages-howto.md`](capability-packages-howto.md) says how to *build*
a capability package. This document says where a package may come **from**, how its bytes
reach the platform, and who authorises an install.

Three things are worth stating before the detail, because they are the shape of the whole
design:

1. **A chat agent can ask for an install. It can never perform one.** A request fetches
   and previews; a human decides on a one-time link; the platform installs afterwards,
   with no model in the loop.
2. **No fetched code runs before that decision.** Not to read a manifest, not to list a
   package's chat tools, not to build the preview the operator reads. The preview is
   `yaml.safe_load` plus `ast.parse` plus a hash, and the git fetcher is dulwich, which
   runs no hooks.
3. **The trust anchor is displayed provenance plus operator consent.** There is no
   signature check (see [Limits](#limits)). What the operator approves is a named source,
   a resolved revision and a tree hash, all recorded.

---

## 1. Source specs

A *source spec* is the one string that says where a package comes from. The grammar is
closed — `carpenter/packages/sources.py` accepts these forms and nothing else:

| Form | Example | Notes |
|---|---|---|
| Bare name | `hello` | Resolved through the operator's [source registry](#2-the-source-registry); falls back to the legacy local tree when no source is configured |
| Local directory | `/srv/pkgs/hello`, `file:///srv/pkgs/hello` | The directory holding `manifest.yaml`. `~` is expanded |
| Git repository | `git+https://forge.example/org/packages.git@v0.2.0#subdir=packages/hello` | `@ref` is a branch, tag or commit; `#subdir=` locates the package inside a monorepo |
| Release archive | `https://forge.example/org/pkg/releases/download/v0.2.0/hello.tar.gz#sha256=<64 hex>` | `.tar.gz`, `.tgz` and `.tar`; the digest pin is optional but recommended |

What is refused, and why:

- **`git+ssh://`, `ssh://`, `git://`** — never accepted. They carry no transport integrity
  the platform can reason about, and the first two depend on an agent or key setup outside
  the platform's control.
- **Plain `http://`** — accepted only for a loopback literal (`127.0.0.1`, `localhost`,
  `[::1]`), so a test can run a throwaway server without a certificate.
- **Zip archives** — refused with a message naming the expected format. Only tar is
  extracted, by one hardened extractor.
- **A credential inside the spec** — a `userinfo` component or a token query parameter is
  stripped from the canonical URL and shown as `***` in anything stored or logged. A read
  token belongs in a [registry entry's `credential_ref`](#2-the-source-registry).

Parsing is pure: no network, no package tree read, no code. A spec can be parsed, shown to
an operator and compared long before any bytes are fetched.

### Identity

`source_id(spec)` hashes the *logical* source — kind, canonical location, subdir — and
deliberately ignores the ref. Two remotes that both publish a package called `hello` are
therefore distinguishable, and the pristine-archive cache that upgrades merge against is
keyed by logical source rather than by `(name, version)`.

---

## 2. The source registry

A bare name is resolved against the top-level `package_sources` list:

```yaml
package_sources:
  - name: house                                   # the alias, used in previews and logs
    url: git+https://forge.example/org/packages.git
    subdir_template: packages/{name}              # omit when the repo IS one package
    credential_ref: HOUSE_FORGE_TOKEN             # a variable NAME, never a value
```

- **The list order is the search order.** There is no second key that can disagree with it.
- `subdir_template` may contain `{name}`; that is how a monorepo of many packages is
  addressed, and it is also what makes the source [listable](#7-discovery).
- `credential_ref` names a platform-provided environment variable. The platform injects
  the value at fetch time, to one origin, and never persists or logs it. A package cannot
  supply or override it.
- A malformed entry is **refused with a clear error, not skipped**, so a typo cannot
  silently change which remote a package comes from.
- Empty (the default) keeps the pre-registry behaviour: a bare name resolves to the local
  source tree.

The registry is top-level and deliberately *not* nested under `packages:`, which is a
mapping whose every key is a package name. Nesting it there would reserve the registry's
keys as package names, and because the YAML layer replaces a top-level value wholesale, an
operator setting one per-package override would silently drop the whole registry.

### Reachability

A package fetch goes through the platform's one network allowlist,
`carpenter/security/egress.py`, like any other outbound request. Anything not globally
routable — a self-hosted forge on a private address, for instance — needs its host in
`web_egress_allowlist`:

```yaml
web_egress_allowlist:
  - forge.example
```

The refusal names both the host and the config key, so an operator is never left guessing.
No third allowlist concept was invented for packages.

---

## 3. Fetching

`carpenter/packages/fetch.py` is the only entry point. It owns the egress gate and
dispatches on the spec's kind; `fetch_git.py` and `fetch_archive.py` register against it.
Anything that fetches a package goes through here, which is what keeps the guarantees
below from being per-caller promises.

**Quarantine first.** A fetch writes into a fresh per-call directory under
`{base_dir}/cache/package-staging/`, which is on no registry search path. Nothing is
installed from the quarantine until the manifest and security checks have passed, and the
caller can delete the whole thing whatever happens next.

**Caps are the fetcher's business.** Every fetcher enforces the same `FetchLimits`
(defaults: 64 MiB total, 5 000 files, 16 MiB per file, 60 s wall clock; tightenable under
`package_install`). A remote source is untrusted input, so an unbounded download, a
file-count explosion or a hung connection fails rather than filling the disk. An archive's
tar index is walked **one member at a time** and the first cap passed stops the walk,
because reading a whole index is itself the bomb when an archive declares millions of
members. What actually landed is then re-measured, because the index is attacker-supplied
too.

**No remote code executes.** The git fetcher is dulwich — the `git` binary is never
invoked, so no hooks and no filters run, and the clone is given an empty git configuration
so the host's `~/.gitconfig` cannot rewrite the URL after the egress gate has already
checked it. The clone is made with `checkout=False`, submodules are refused, only the
package subtree is materialised, every entry's mode and path is checked, symlink escapes
and non-regular members are refused, and `.git` never reaches the installed tree.

**Every redirect hop is re-checked.** The gate the dispatcher ran sees one URL, so both
fetchers drive redirects by hand through `egress.check_redirect`, cap the chain at 10, and
refuse a hop that leaves the scheme, host and port the request named. Without that, a 302
from an allowed host to a loopback, link-local or cloud-metadata address would walk
straight through the gate — and the provenance shown to the operator would name the
requested host rather than the one the bytes came from.

**The ambient environment cannot redirect the transport.** Both fetchers ignore
`HTTP_PROXY` and friends (`trust_env=False`; the git transport builds its own pool
manager for the same reason). A proxy is a destination the gate never resolved, so what
the gate checked would not be what gets connected to.

**What was fetched is pinned.** `FetchResult.resolved` is the commit sha40 for git, the
archive's sha256 for an archive, and empty for a local directory — there is nothing to pin
when the operator's own tree is the anchor. An archive's digest is reported whether or not
a `#sha256=` pin was given; when it was not, the digest is logged at WARNING so an
operator can paste it back into the spec.

---

## 4. The pre-consent preview

`carpenter/packages/static_preview.py` answers *what would installing this give the chat
agent?* with `ast.parse` alone. Nothing imports, `exec`s, `eval`s or compiles the tree.

The obvious implementation — import the package's chat-tool modules and read the metadata
the `@chat_tool` decorator attached, which is what
`installer.classify_package_chat_tools` does — executes module-level code. For a source
the operator has not yet agreed to trust, *that preview is the exploit*: arbitrary remote
code would run before the prompt meant to inform the decision. **Never call
`classify_package_chat_tools` on a quarantined tree.**

The price is that only literal decorator arguments can be read. Anything else is reported
as unresolved and never guessed, and a tool whose capabilities could not be read
statically is treated as **write-capable** — fail closed.

---

## 5. Chat-requested installs

Two chat tools, in `config_seed/chat_tools/packages_install_request.py`:

```python
list_available_packages()                  # which sources exist, and how many packages each offers
list_available_packages(source="house")    # that source's packages, each with an install spec
request_package_install(sources=[...])     # ask a human to approve installing these
```

`request_package_install` resolves the specs, fetches them into quarantine, previews them
without executing anything, writes a `pending_package_installs` row, and returns a
one-time URL. The agent's job is to hand that URL to the person who asked, and stop.

### Why a link rather than a confirmation prompt

The platform's chat confirmation seam is synchronous, with no timeout and no conversation
id, and an operator's "yes" typed in chat while a turn is blocked is persisted and then
dropped — `deliver_inbound` starts no task while a turn is pending. A blocking
confirmation handler therefore *cannot* work here, which is also why the older
`install_package` tool is operator-only and refused when no confirmation handler is
registered. The one proven human yes/no mechanism on this platform is a one-time link.

### The approval page

`GET /api/package-install/<token>` renders, per package: where the bytes came from
(source kind, redacted URL, requested ref, resolved commit or digest, subdir, fetch time),
the tree hash, the manifest's own declarations, any `platform_capabilities` it wants with
a **default-unchecked** grant checkbox, credential requirements, the statically-read chat
tools, and whether a package of that name is already installed. Every value is escaped on
its way into the HTML: a package description and the request text are
attacker-influenced — a third party talking the agent into asking is the whole threat
model.

Three rules the page enforces:

- **A GET mutates nothing**, except marking a row whose TTL has run out as expired. There
  is no approve-on-GET, because an email client preloading an image must not install
  software.
- **The page is a decision surface, not an input surface.** The only thing the browser
  sends back is the grant checkboxes. Destination, package name, spec and tree hash all
  come from the recorded row.
- **An unticked grant still installs the package**, with no capability verbs registered.
  Consent is per capability, not all-or-nothing.

### Reading is open; deciding needs authentication

`GET` is exempt from the UI-token middleware so the link opens from whatever chat app or
mail client carried it. `POST /api/package-install/<token>/decide` is **not** exempt: it
needs a session authenticated to the platform *in addition to* the single-use token.

This is a deliberate departure from the arc-review links, where the unguessable uuid4 in
the URL is the whole credential. The reason is not link hygiene, it is what the link
authorises. The approval URL is returned to the chat agent that asked and is persisted in
the conversation, and the platform ships an HTTP POST dispatch verb — so the model that
requested the install would otherwise hold the credential that approves it, and a
prompt-injected agent could request and approve in one move, cutting out the human the
whole design rests on. (`hidden=True` hides a message from the UI, not from the model, so
it cannot help.) Approving an install runs unsandboxed code in the platform process with
the platform's credentials and egress, which is not comparable to approving a reviewed arc
step.

### State machine

```
requested ──▶ approved ──▶ installing ──▶ installed
     │            │             │      └─▶ failed
     │            └─────────────┴────────▶ failed
     ├──▶ rejected
     └──▶ expired
```

The four terminal states are never re-entered or left, so a decision cannot be replayed
and a second POST cannot install twice; a replayed link gets `410`. The row lives in SQL
rather than in memory because a request outlives the turn that made it and must outlive a
restart — and a package install has no arc to hang durability off. `approved → installing`
is what makes "interrupted mid-install" detectable: a row still in `installing` with no
live worker was interrupted, and the honest move is to fail it rather than guess what
landed on disk.

The token is the credential: a uuid4, single-use, expiring, and logged only as its first
eight characters.

### The install itself

Approving flips the row and enqueues `package.install.approved`; the work handler
(`install_approved_handler.py`) performs the install on a worker thread. The route does not
install, because a 10–60 s fetch-verify, copytree and hash on the event loop would stall
every other request including the chat UI's poll, and because a browser refresh of a
synchronous install opens a second install window. The work item's idempotency key and the
row's state machine are the interlock.

The batch (`install_batch.py`) guarantees:

- **The operator approved specific bytes.** Each staged tree is re-hashed and compared
  against the recorded `tree_hash` before anything is installed. A mismatch is a refusal,
  never a re-fetch — re-fetching at approval time is a TOCTOU hole where the operator
  approves tree H and a hostile remote serves H′ to the second fetch. A vanished tree is
  also a refusal: ask again.
- **"Install X and Y" cannot half-fail silently.** One approval covers the batch, each
  package installs in its own transaction, and on the first failure the batch uninstalls
  only what it installed **fresh** — never one it merely upgraded. The per-package report
  names any `security_policies` entries a rollback leaves behind, because that allowlist is
  a one-way ratchet by design.

The outcome is written back into the originating conversation as one visible system
message — **zero model calls**, one INSERT, rendered by the UI's existing poll, and pushed
to a non-web channel if the conversation came in over one. Re-invoking the agent to
paraphrase an already-exact report costs a model call per approval, so it is off by
default (`package_install.notify_agent`).

### Limits on asking

The only defence that cannot be hardened is the human reading the page, so a request the
agent can repeat without limit is a retry loop against their patience:

| `package_install` key | Default | Meaning |
|---|---|---|
| `request_ttl_hours` | 24 | Link lifetime; an expired link gets `410` |
| `approved_grace_hours` | 6 | Give up on an approval whose install never ran |
| `max_open_requests` | 3 | Undecided requests allowed at once |
| `max_requests_per_hour` | 5 | Requests the agent may create per hour |
| `max_sources_per_request` | 5 | Packages in one approval |
| `notify_agent` | `false` | Re-invoke the chat agent on the outcome |

A rejected source is also remembered for 24 hours, so "ask again immediately" is refused.
Requests are never auto-approved, whatever these are set to. An hourly sweep expires
undecided requests, reclaims the quarantine each was holding, and re-queues an approval
whose install never started (the enqueue happens after the approval commits, so a crash in
between would otherwise wedge the request).

---

## 6. Provenance, hot-load and upgrades

### What is recorded

`installed_packages` carries the source alongside the hash:

| Column | Meaning |
|---|---|
| `source_kind` | `local`, `git` or `archive` |
| `source_url` | Canonical URL, **redacted** |
| `source_ref` | The branch, tag or commit the operator asked for |
| `source_resolved` | What was actually fetched: commit sha40, or archive sha256 |
| `source_subdir` | POSIX path of the package root inside the fetched tree |
| `source_id` | Stable id of the logical source (kind + location + subdir) |
| `fetched_at` | When the bytes were fetched |
| `shipped_hash` | Root hash of the pristine tree this version shipped |

A package is therefore identified by its source, not by its name: two remotes publishing
the same name cannot be confused, and a later run can tell whether it would get the same
bytes.

### Loading without a restart

`reload_packages()` registers a package installed for the **first time** in this process
live, by calling the same registry methods startup calls. A package whose modules are
already cached in `sys.modules` cannot be reloaded and is honestly reported as
`needs_restart: true` — re-registering it would run the *old* module bodies while the
operator believed the new ones were live. Purging `sys.modules` is not a fix: it would make
the next import read the new files but retract nothing built from the old ones, and the
platform holds many such references (bound tool objects, JUDGE handlers, data-model
classes).

### Upgrades

```bash
python3 -m carpenter packages update <name> [--yes] [--accept-conflicts]
```

An update takes no source: it re-fetches the one the install recorded, **at its ref**
rather than at the commit it was pinned to, because "follow the branch and tell me what
changed" is the point. A package with no recorded remote source is refused, with the
command that fixes it.

The installed tree is a working directory an operator may legitimately have edited, and
re-installing over it clobbers those edits silently. So an update classifies every path
across three trees — the pristine tree the installed version shipped (from the local
archive cache), the tree on disk now, and the pristine tree the new version ships — and
applies the result through the platform's one merge engine. A file only the operator
touched is kept; a file only the new version changed is taken; a file both touched is a
**conflict and refuses the update** unless `--accept-conflicts` says otherwise. Where the
pristine anchor is missing, the plan degrades to an explicit clobber that lists what it
will overwrite rather than pretending to merge.

Capability grants and trigger subscriptions survive an update. The grants are read back
and re-applied rather than silently revoked (a verb the new version drops still
disappears). The subscriptions are re-registered after the swap, because
`_subscriptions.json` lives *inside* the tree being replaced and the reconcile treats it as
generated rather than as an operator's file — without that step a package's triggers
stopped firing after any update.

---

## 7. Discovery

`list_available_packages` (`carpenter/packages/discover.py`) answers the question a person
actually asks first — *what can I install from here?* — so that "install the email package"
is a sentence someone can say without first finding a URL to paste.

Three properties make it safe to expose to an agent:

- **Metadata only.** It reads each candidate's `manifest.yaml` and nothing else. No module
  is imported, and the static preview is deliberately *not* run either: a preview is what
  an operator reads on an approval page, after a specific install has been asked for.
- **One fetch path.** Everything goes through `fetch_source`, so the egress gate, the
  quarantine, the caps, the credential injection and the tree hardening all apply — and the
  quarantine is always removed again. A listing leaves nothing behind.
- **It cannot be pointed anywhere.** The only input is the alias of a source the operator
  already configured, which is what keeps it out of SSRF territory. The listing cache's TTL
  (`package_sources_listing_ttl_hours`, default 6) bounds how often a caller can make the
  platform talk to a remote at all: an agent asking the same question all afternoon costs
  at most one fetch per source per TTL.

What gets listed follows from `subdir_template`: absent, the fetched root is one package;
a fixed template (`pkg/core`) names one package; a template containing `{name}`
(`packages/{name}`) makes the children of the directory above the placeholder the
candidates. Each listed package comes with the exact spec string to install it, pinned to
the revision the listing was read from — prefer those over a hand-written spec, so the
person approving sees the same bytes the agent described.

---

## 8. Limits

Stated plainly, because each is a thing an operator might otherwise assume:

- **No signatures.** The trust anchor is displayed provenance plus operator consent. The
  single place a signature or attestation check would slot in is `fetch_source`.
- **No operator identity.** Requiring platform authentication to approve is as far as this
  goes; "the operator" means "whoever can sign in". There is no per-person audit of who
  approved what beyond that.
- **No python dependency installation.** The manifest has no field for third-party python
  requirements and nothing is ever installed with pip; a package's imports must be
  satisfied by the platform's own environment.
- **A grant is per install, not per use.** Approving `platform_capabilities` registers the
  verbs; nothing re-asks when they are later used.
- **`security_policies` entries are a one-way ratchet.** A rollback names what it leaves
  behind rather than silently narrowing an allowlist another package may now depend on.

---

## 9. Where to look

| Path | What lives there |
|---|---|
| `carpenter/packages/sources.py` | The spec grammar, redaction, `source_id`, the registry reader |
| `carpenter/packages/fetch.py` | The fetch contract, the egress gate, the quarantine, the local fetcher |
| `carpenter/packages/fetch_git.py` | dulwich clone, ref resolution, subtree materialisation, tree hardening |
| `carpenter/packages/fetch_archive.py` | Streamed download, digest pinning, lazy tar-index capping |
| `carpenter/packages/static_preview.py` | The AST-only chat-tool preview |
| `carpenter/packages/install_requests.py` | Resolve → fetch → preview → row → one-time URL |
| `carpenter/packages/pending_install.py` | The request row, its DDL and its state machine |
| `carpenter/api/package_install.py` | The approval page and the decide route |
| `carpenter/api/auth.py` | Why the GET is open and the POST is not |
| `carpenter/packages/install_approved_handler.py` | The `package.install.approved` work handler |
| `carpenter/packages/install_batch.py` | Per-package transactions, hash re-verification, compensating rollback |
| `carpenter/packages/install_notify.py` | The zero-model outcome message |
| `carpenter/packages/reload.py` | Live load, and when it refuses |
| `carpenter/packages/update.py` | Re-fetch at the recorded ref, three-way reconcile |
| `carpenter/packages/discover.py` | Listing a configured source |
| `config_seed/chat_tools/packages_install_request.py` | The two chat tools |
| `config_seed/kb/self-modification/capability-packages.md` | The same workflow, written for the agent |
