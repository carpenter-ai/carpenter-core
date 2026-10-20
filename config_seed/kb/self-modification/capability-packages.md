# Installing Capability Packages From A Remote Source

How a capability package gets from a remote repository or archive onto this platform, when someone asks for one in chat. Nothing in this flow installs anything by itself: a person approves the install on a one-time link.

## The operator configures a source once

```yaml
package_sources:
  - name: house                              # the alias you refer to it by
    url: git+https://forge.example/org/packages.git
    subdir_template: packages/{name}         # omit when the repo IS one package
    credential_ref: HOUSE_FORGE_TOKEN        # optional; a variable NAME, never a value
web_egress_allowlist:
  - forge.example                            # needed for anything not publicly routable
```

`package_sources` says where packages may come from; `web_egress_allowlist` is the platform's one network allowlist and a package fetch goes through it like any other outbound request. A self-hosted forge on a private network is refused until its host is listed, and the refusal names both the host and the key.

## Then, in chat

```python
list_available_packages()                    # which sources exist, and how many packages each offers
list_available_packages(source="house")      # that source's packages, each with an install spec
request_package_install(sources=["git+https://forge.example/org/packages.git@<commit>#subdir=packages/<name>"])
```

`list_available_packages` is read-only: it reads each candidate's `manifest.yaml` and nothing else, runs none of the package's code, and tells you which commit or digest it read and when. Its listings are cached, so counts with no `source` argument come from the cache rather than a fresh fetch.

`request_package_install` fetches the named sources into a quarantine directory, builds a preview without executing any of the fetched code, and returns a one-time approval URL. Give that URL to the person who asked and stop. They see where the bytes came from, the tree hash, what the package declares and which platform capabilities it wants, and they approve or reject the whole set. The platform performs the install itself afterwards and reports back here.

## Notes

- A source spec is `git+https://host/org/repo.git[@ref][#subdir=path]`, an `https://host/path/pkg.tar.gz[#sha256=...]` archive, or a bare package name once a source is configured. `ssh://` and `git://` are never accepted.
- Prefer the spec string a listing gave you: it pins the exact revision the listing was read from, so the person approving sees the same bytes you described.
- A credential for a private source comes from the platform, by the variable name the source declares. A package can never supply one, and it is never persisted or logged.
- A package already loaded in this process needs a restart before a reinstall takes effect; a newly installed one can be loaded live.

## Related
[[security/trust-boundaries]] · [[self-modification/config-tools]] · [[web/trust-warning]]
