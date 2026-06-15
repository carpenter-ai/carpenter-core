"""Capability-package manifest format and YAML loader.

Each capability package ships a ``manifest.yaml`` at the package root
declaring its identity and the artifacts it contributes.  Phase A
recognised a deliberately-small set of fields; D24 (B-min stage 3a)
extends the schema with declaration-only fields for arc templates,
JUDGE handlers, data models, KB articles, and trigger subscriptions.
Most of these fields are validated at manifest-load time but not yet
*acted on* — Stage 3b connects the wires from declaration to runtime
registration.

Manifest schema (Phase A + D24 stage 3a):

```yaml
name: hello                      # required, [a-z][a-z0-9_-]{0,63}
version: "0.1.0"                 # required, free-form (semver-ish)
description: |                   # required, human-readable summary
  One-line summary, expanded as needed.
chat_tools:                      # optional, list of relative paths to *.py
  - tools.py                     #   modules containing @chat_tool defs
kb_namespace: hello              # optional, restricts where KB articles
                                 #   may be seeded (Phase B+)
platform_compatibility:          # optional, defaults to ["any"]
  - any

# D24 / Stage 3a additions — declaration-only this PR.

arc_templates:                   # optional list of arc-template refs
  - name: email-triage           #   template name (flat/unprefixed, SD7)
    path: templates/email-triage/template.yaml
    briefing_kind: EmailReviewBriefing
    extract_kind: EmailReviewExtract
    judge_handler: judges.email_review:judge_email_review

judge_handlers:                  # optional list of judge-handler refs
  - name: judge_email_review
    module: judges.email_review

data_models:                     # optional list of dataclass names
  - EmailReviewBriefing          #   exported from <pkg>/data_models.py
  - EmailReviewExtract

kb_articles:                     # optional list of kb-article refs
  - path: kb/email/overview.md
    slug: email/overview

trigger_subscriptions:           # optional list of subscription refs
  - event: email.received
    handler: handlers.fetch:run

# D24 / B-full: package-proposed allowlist additions.
allowlist_proposals:             # optional list of allowlist entries
  - type: domain                 #   one of POLICY_TYPES (typo defense)
    value: example.com           #   merged into SecurityPolicies on
                                 #   install (after operator confirm);
                                 #   uninstall does NOT remove (SD5).

# Packages may declare credential requirements.  Field is named
# ``credential_requirements`` (NOT ``credentials``) because the security
# gate forbids the bare word ``credentials`` as a top-level key — that
# historically meant bundled credential *bytes*.  Two kinds are
# supported: ``oauth`` (OAuth 2.0 authorization-code) and ``env`` (plain
# env-var credentials, e.g. an IMAP/SMTP app password).
credential_requirements:         # optional list of credential reqs
  - kind: oauth                  #   OAuth 2.0 authorization-code
    provider: google             #   human-readable provider tag
    env_key_prefix: GMAIL_OAUTH  #   UPPER_SNAKE; produces _ACCESS_TOKEN,
                                 #   _REFRESH_TOKEN, _TOKEN_EXPIRES_AT,
                                 #   _CLIENT_ID, _CLIENT_SECRET, _TOKEN_URL
    authorize_url: https://accounts.google.com/o/oauth2/v2/auth
    token_url: https://oauth2.googleapis.com/token
    scopes:
      - https://www.googleapis.com/auth/gmail.readonly
  - kind: env                    #   plain env-var credentials
    provider: imap_smtp          #   human-readable provider tag
    env_key_prefix: IMAP_EMAIL   #   UPPER_SNAKE; full var is
                                 #   f"{env_key_prefix}_{suffix}"
    required_keys:               #   non-empty, UPPER_SNAKE suffixes
      - IMAP_HOST
      - IMAP_PORT
      - SMTP_HOST
      - SMTP_PORT
      - USERNAME
      - PASSWORD
```

Stage 3a behaviour for the new fields:

* ``arc_templates``, ``judge_handlers``, ``data_models``,
  ``kb_articles`` — manifest is validated for shape and the referenced
  filesystem paths (where applicable) are checked for existence under
  the package root.  Loading the actual templates / handlers / data
  models / articles into runtime registries is Stage 3b.
* ``trigger_subscriptions`` — validated for shape only.  Wiring into
  the trigger pipeline is Stage 3b.

Security guards live in :mod:`carpenter.packages.security`; this module
is intentionally only concerned with shape/well-formedness so that the
two layers can be reasoned about independently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Manifest fields recognised after D24 stage 3a.  Anything outside this
# set is rejected at load time — accepting unknown fields would let a
# future package smuggle declarations past validators that haven't
# learned about them yet (defense-in-depth for I10).
_REQUIRED_FIELDS = frozenset({"name", "version", "description"})
_OPTIONAL_FIELDS = frozenset({
    "chat_tools",
    "kb_namespace",
    "platform_compatibility",
    # D24 stage 3a — declaration-only this PR.
    "arc_templates",
    "judge_handlers",
    "data_models",
    "kb_articles",
    "trigger_subscriptions",
    # D24 Phase 3a PR-B — package-shipped triggers (in-process event
    # sources).  Distinct from ``trigger_subscriptions``: the latter
    # *consumes* events, the former *produces* them.  Each entry names
    # a class in a ``triggers/<file>.py`` module within the package.
    "triggers",
    # D24 B-full — packages may propose flat-global allowlist additions
    # that the operator confirms at install time (SD5: one-way ratchet,
    # uninstall does NOT remove them).
    "allowlist_proposals",
    # Phase 0 (OAuth-callback): packages declare credential requirements
    # (currently only OAuth 2.0 authorization-code).  See
    # _parse_credentials below.
    "credential_requirements",
})
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS

# Package names: lowercase ASCII, digits, hyphen, underscore.  Mirrors
# the conventions used for KB paths and config_seed subdirectories so
# that names are safe to embed in URLs, file paths, and log lines.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Identifier matcher used for handler / template / dataclass names
# declared in the manifest.  Slightly more permissive than
# ``_NAME_RE`` (PascalCase dataclasses are allowed) but still strict
# about not embedding paths or shell metacharacters.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")


class ManifestError(ValueError):
    """Raised when a manifest fails shape/well-formedness validation."""


@dataclass(frozen=True)
class ArcTemplateRef:
    """A reference to an arc-template that the package ships.

    Attributes:
        name: Template name (flat, unprefixed; SD7).  Stage 3b will
            register the template under this name.
        path: Path within the package directory to the template YAML.
        briefing_kind: Optional dataclass name (in ``data_models``) for
            the trusted PLANNER → REVIEWER briefing Resource (SD11).
        extract_kind: Optional dataclass name (in ``data_models``) for
            the REVIEWER → JUDGE pending extract Resource (SD11).
        judge_handler: Optional ``module:func`` reference into the
            package's JUDGE handler module.
    """

    name: str
    path: str
    briefing_kind: str | None = None
    extract_kind: str | None = None
    judge_handler: str | None = None


@dataclass(frozen=True)
class JudgeHandlerRef:
    """A reference to a JUDGE handler module the package ships.

    Attributes:
        name: Logical handler name (used in ``arc_templates`` references).
        module: Python import path within the package, e.g.
            ``judges.email_review`` for ``<pkg>/judges/email_review.py``.
    """

    name: str
    module: str


@dataclass(frozen=True)
class KbArticleRef:
    """A reference to a KB article shipped by the package.

    Attributes:
        path: Path within the package directory to the article file.
        slug: Slug under the package's ``kb_namespace`` that the
            article will be served at (Stage 3b loads articles).
    """

    path: str
    slug: str


@dataclass(frozen=True)
class AllowlistProposal:
    """A package-proposed allowlist entry for a platform policy type.

    Attributes:
        policy_type: One of the platform's known ``POLICY_TYPES``
            (validated at parse time — typo defense).  Packages may
            NOT introduce new policy types in B-full; that is reserved
            for a future PR with a richer ``policies`` field.
        value: The literal allowlist value (e.g. ``"example.com"`` for
            ``policy_type="domain"``).  Normalisation happens when the
            value is added to :class:`SecurityPolicies`.
    """

    policy_type: str
    value: str


@dataclass(frozen=True)
class OAuthCredentialRef:
    """A package-declared OAuth credential requirement.

    Attributes:
        kind: Always ``"oauth"`` today (placeholder for future
            ``api_key``, ``basic_auth`` flavours).
        provider: Human-readable provider tag (``"google"``,
            ``"slack"``, ...).  Used only in logs and the chat-side
            authorize-link prompt.
        env_key_prefix: ``UPPER_SNAKE`` prefix under which the platform
            stores access/refresh/expires-at tokens after a successful
            callback.  Must match ``[A-Z][A-Z0-9_]{0,63}``.
        authorize_url: Provider's authorization endpoint URL (must be
            absolute https://).
        token_url: Provider's token endpoint URL (must be absolute
            https://).
        scopes: Tuple of OAuth scope strings.
    """

    kind: str
    provider: str
    env_key_prefix: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class EnvCredentialRef:
    """A package-declared environment-variable credential requirement.

    Unlike :class:`OAuthCredentialRef`, this describes a credential set
    supplied directly as environment variables (e.g. an IMAP/SMTP
    mailbox accessed with a plain app password — host, port, username,
    password).  There is no authorization flow; the operator provides
    the values at install time (a future PR wires up ``.env`` writing
    and runtime loading — this PR only declares and validates the
    schema).

    Attributes:
        kind: Always ``"env"``.
        provider: Human-readable provider tag (e.g. ``"imap_smtp"``).
            Used only in logs and operator-facing prompts.
        env_key_prefix: ``UPPER_SNAKE`` prefix under which the platform
            stores the credential values.  Must match
            ``[A-Z][A-Z0-9_]{0,63}``.  The full variable name for a
            given suffix is conceptually ``f"{env_key_prefix}_{suffix}"``.
        required_keys: Tuple of ``UPPER_SNAKE`` env-var suffixes the
            package needs (e.g. ``("IMAP_HOST", "IMAP_PORT",
            "USERNAME", "PASSWORD")``).  Each must match
            ``[A-Z][A-Z0-9_]*`` and the set must be non-empty with no
            duplicates.
    """

    kind: str
    provider: str
    env_key_prefix: str
    required_keys: tuple[str, ...]


@dataclass(frozen=True)
class SubscriptionRef:
    """A reference to a trigger subscription the package contributes.

    Attributes:
        event: Event name to subscribe to (no allowlist; Q4).
        handler: ``module:func`` reference into the package's handlers.
    """

    event: str
    handler: str


@dataclass(frozen=True)
class TriggerRef:
    """A reference to an in-process Trigger class the package ships.

    Attributes:
        name: Unique instance name for the trigger (within the package's
            keyspace; the installer namespaces this further when
            registering with the trigger registry).
        type: The ``trigger_type()`` string the trigger class returns.
            Must match the class registered from ``module``.
        module: Path within the package directory to the .py file
            defining the Trigger subclass.  Relative path; ``..`` and
            absolute paths are rejected at parse time.
        config: Optional per-instance configuration dict passed to the
            trigger's constructor.  Trigger-type-specific shape; the
            manifest layer does not validate its contents.
        enabled: Whether to instantiate at install time (default True).
            Disabled triggers still record the type so a future config
            tweak can enable them without re-installing.
    """

    name: str
    type: str
    module: str
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class PackageManifest:
    """Parsed, shape-validated capability-package manifest.

    Attributes:
        name: Package identifier; matches ``^[a-z][a-z0-9_-]{0,63}$``.
        version: Free-form version string (semver-ish recommended).
        description: Human-readable description.
        chat_tools: Relative paths (within the package directory) to
            Python modules holding ``@chat_tool``-decorated functions.
            Empty list means the package ships no chat tools.
        kb_namespace: Optional KB sub-namespace the package is allowed
            to seed into (Phase B+).  Defaults to ``name``.
        platform_compatibility: List of platform tags the package is
            valid on (``"any"`` matches everything).  Phase A doesn't
            filter on this yet but the field is required to round-trip.
        arc_templates: D24 stage 3a — declared template refs.  Loaded
            into the template registry in stage 3b.
        judge_handlers: D24 stage 3a — declared JUDGE handler refs.
            Loaded into the handler registry in stage 3b.
        data_models: D24 stage 3a — dataclass names exported from a
            ``data_models.py`` module within the package.  Loaded
            in stage 3b.
        kb_articles: D24 stage 3a — declared KB article refs.  Loaded
            in B-full.
        trigger_subscriptions: D24 stage 3a — trigger subscriptions
            the package contributes.  Wired into the trigger pipeline
            in stage 3b.
        credential_requirements: Declared credential requirements.  Each
            entry is either an :class:`OAuthCredentialRef`
            (``kind: oauth``) or an :class:`EnvCredentialRef`
            (``kind: env``, plain env-var credentials such as an
            IMAP/SMTP app password).  Declaration/validation only at
            this layer; install-time ``.env`` writing and runtime
            loading are handled elsewhere.
        source_path: Absolute path to the package directory on disk.
            Set by :func:`load_manifest`; not part of the on-disk
            manifest itself.
    """

    name: str
    version: str
    description: str
    chat_tools: tuple[str, ...] = ()
    kb_namespace: str = ""
    platform_compatibility: tuple[str, ...] = ("any",)
    arc_templates: tuple[ArcTemplateRef, ...] = ()
    judge_handlers: tuple[JudgeHandlerRef, ...] = ()
    data_models: tuple[str, ...] = ()
    kb_articles: tuple[KbArticleRef, ...] = ()
    trigger_subscriptions: tuple[SubscriptionRef, ...] = ()
    triggers: tuple[TriggerRef, ...] = ()
    allowlist_proposals: tuple[AllowlistProposal, ...] = ()
    credential_requirements: tuple[
        OAuthCredentialRef | EnvCredentialRef, ...
    ] = ()
    source_path: Path = field(default_factory=Path)


def _ensure_list_of_str(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestError(
            f"Manifest field {field_name!r} must be a list of strings, "
            f"got {type(value).__name__}",
        )
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ManifestError(
                f"Manifest field {field_name!r}[{i}] must be a non-empty "
                f"string, got {item!r}",
            )
        out.append(item)
    return tuple(out)


def _ensure_list_of_dict(value: Any, field_name: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError(
            f"Manifest field {field_name!r} must be a list of mappings, "
            f"got {type(value).__name__}",
        )
    out: list[dict] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ManifestError(
                f"Manifest field {field_name!r}[{i}] must be a mapping, "
                f"got {type(item).__name__}",
            )
        out.append(item)
    return out


def _check_relative_path(field_name: str, raw: Any, idx: int) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError(
            f"Manifest field {field_name!r}[{idx}].path must be a "
            f"non-empty string, got {raw!r}",
        )
    p = Path(raw)
    if p.is_absolute():
        raise ManifestError(
            f"Manifest field {field_name!r}[{idx}].path {raw!r} must be "
            f"relative to the package root",
        )
    parts = p.parts
    if any(part == ".." for part in parts):
        raise ManifestError(
            f"Manifest field {field_name!r}[{idx}].path {raw!r} must "
            f"not contain '..' components",
        )
    return raw


def _check_ident(field_name: str, raw: Any, idx: int, *, key: str) -> str:
    if not isinstance(raw, str) or not _IDENT_RE.match(raw):
        raise ManifestError(
            f"Manifest field {field_name!r}[{idx}].{key} must match "
            f"{_IDENT_RE.pattern!r}, got {raw!r}",
        )
    return raw


def _check_module_path(
    field_name: str, raw: Any, idx: int, *, key: str,
) -> str:
    """Validate a Python module dotted path (no leading dot, no '..')."""
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError(
            f"Manifest field {field_name!r}[{idx}].{key} must be a "
            f"non-empty string, got {raw!r}",
        )
    if raw.startswith(".") or ".." in raw:
        raise ManifestError(
            f"Manifest field {field_name!r}[{idx}].{key} must not start "
            f"with '.' or contain '..', got {raw!r}",
        )
    for part in raw.split("."):
        if not part or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part):
            raise ManifestError(
                f"Manifest field {field_name!r}[{idx}].{key} component "
                f"{part!r} is not a valid Python identifier (in {raw!r})",
            )
    return raw


def _check_handler_ref(
    field_name: str, raw: Any, idx: int, *, key: str,
) -> str:
    """Validate a ``module:func`` handler reference."""
    if not isinstance(raw, str) or ":" not in raw:
        raise ManifestError(
            f"Manifest field {field_name!r}[{idx}].{key} must be of the "
            f"form 'module:function', got {raw!r}",
        )
    module_part, _, func_part = raw.partition(":")
    _check_module_path(field_name, module_part, idx, key=f"{key}(module)")
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", func_part):
        raise ManifestError(
            f"Manifest field {field_name!r}[{idx}].{key} function part "
            f"{func_part!r} is not a valid identifier (in {raw!r})",
        )
    return raw


def _parse_arc_templates(
    raw_value: Any, *, source_path: Path,
) -> tuple[ArcTemplateRef, ...]:
    items = _ensure_list_of_dict(raw_value, "arc_templates")
    out: list[ArcTemplateRef] = []
    seen_names: set[str] = set()
    allowed_keys = {
        "name", "path", "briefing_kind", "extract_kind", "judge_handler",
    }
    for i, item in enumerate(items):
        unknown = set(item.keys()) - allowed_keys
        if unknown:
            raise ManifestError(
                f"arc_templates[{i}] has unknown keys: {sorted(unknown)}; "
                f"allowed: {sorted(allowed_keys)}",
            )
        missing = {"name", "path"} - set(item.keys())
        if missing:
            raise ManifestError(
                f"arc_templates[{i}] missing required keys: "
                f"{sorted(missing)}",
            )
        name = _check_ident(
            "arc_templates", item.get("name"), i, key="name",
        )
        if name in seen_names:
            raise ManifestError(
                f"arc_templates: duplicate template name {name!r}",
            )
        seen_names.add(name)
        path = _check_relative_path(
            "arc_templates", item.get("path"), i,
        )
        # Existence check — declaration-only at stage 3a, but the path
        # must already exist so packages can't ship dangling references.
        candidate = (source_path / path).resolve()
        try:
            candidate.relative_to(source_path.resolve())
        except ValueError:
            raise ManifestError(
                f"arc_templates[{i}].path {path!r} escapes package root",
            ) from None
        if not candidate.is_file():
            raise ManifestError(
                f"arc_templates[{i}].path {path!r} not found at "
                f"{candidate}",
            )
        briefing_kind = item.get("briefing_kind")
        if briefing_kind is not None:
            _check_ident(
                "arc_templates", briefing_kind, i, key="briefing_kind",
            )
        extract_kind = item.get("extract_kind")
        if extract_kind is not None:
            _check_ident(
                "arc_templates", extract_kind, i, key="extract_kind",
            )
        judge_handler = item.get("judge_handler")
        if judge_handler is not None:
            _check_handler_ref(
                "arc_templates", judge_handler, i, key="judge_handler",
            )
        out.append(ArcTemplateRef(
            name=name,
            path=path,
            briefing_kind=briefing_kind,
            extract_kind=extract_kind,
            judge_handler=judge_handler,
        ))
    return tuple(out)


def _parse_judge_handlers(
    raw_value: Any, *, source_path: Path,
) -> tuple[JudgeHandlerRef, ...]:
    items = _ensure_list_of_dict(raw_value, "judge_handlers")
    out: list[JudgeHandlerRef] = []
    seen_names: set[str] = set()
    allowed_keys = {"name", "module"}
    for i, item in enumerate(items):
        unknown = set(item.keys()) - allowed_keys
        if unknown:
            raise ManifestError(
                f"judge_handlers[{i}] has unknown keys: {sorted(unknown)}; "
                f"allowed: {sorted(allowed_keys)}",
            )
        missing = allowed_keys - set(item.keys())
        if missing:
            raise ManifestError(
                f"judge_handlers[{i}] missing required keys: "
                f"{sorted(missing)}",
            )
        name = _check_ident(
            "judge_handlers", item.get("name"), i, key="name",
        )
        if name in seen_names:
            raise ManifestError(
                f"judge_handlers: duplicate handler name {name!r}",
            )
        seen_names.add(name)
        module = _check_module_path(
            "judge_handlers", item.get("module"), i, key="module",
        )
        # The module must exist as a .py file under the package root.
        rel = Path(*module.split("."))
        candidate = (source_path / rel).with_suffix(".py").resolve()
        try:
            candidate.relative_to(source_path.resolve())
        except ValueError:
            raise ManifestError(
                f"judge_handlers[{i}].module {module!r} resolves outside "
                f"package root",
            ) from None
        if not candidate.is_file():
            raise ManifestError(
                f"judge_handlers[{i}].module {module!r}: source file "
                f"{candidate} not found",
            )
        out.append(JudgeHandlerRef(name=name, module=module))
    return tuple(out)


def _parse_data_models(
    raw_value: Any, *, source_path: Path,
) -> tuple[str, ...]:
    items = _ensure_list_of_str(raw_value, "data_models")
    if not items:
        return ()
    seen: set[str] = set()
    for i, name in enumerate(items):
        if not _IDENT_RE.match(name):
            raise ManifestError(
                f"data_models[{i}] {name!r} must match {_IDENT_RE.pattern!r}",
            )
        if name in seen:
            raise ManifestError(
                f"data_models: duplicate dataclass name {name!r}",
            )
        seen.add(name)
    # The package must ship a ``data_models.py`` module exporting them.
    candidate = (source_path / "data_models.py").resolve()
    try:
        candidate.relative_to(source_path.resolve())
    except ValueError:
        raise ManifestError(
            "data_models requires a data_models.py module at the package "
            "root, but the resolved path escapes the package root",
        ) from None
    if not candidate.is_file():
        raise ManifestError(
            "data_models declared but data_models.py not found at "
            f"{candidate}",
        )
    return items


def _parse_kb_articles(
    raw_value: Any, *, source_path: Path,
) -> tuple[KbArticleRef, ...]:
    items = _ensure_list_of_dict(raw_value, "kb_articles")
    out: list[KbArticleRef] = []
    seen_slugs: set[str] = set()
    allowed_keys = {"path", "slug"}
    for i, item in enumerate(items):
        unknown = set(item.keys()) - allowed_keys
        if unknown:
            raise ManifestError(
                f"kb_articles[{i}] has unknown keys: {sorted(unknown)}; "
                f"allowed: {sorted(allowed_keys)}",
            )
        missing = allowed_keys - set(item.keys())
        if missing:
            raise ManifestError(
                f"kb_articles[{i}] missing required keys: {sorted(missing)}",
            )
        path = _check_relative_path("kb_articles", item.get("path"), i)
        candidate = (source_path / path).resolve()
        try:
            candidate.relative_to(source_path.resolve())
        except ValueError:
            raise ManifestError(
                f"kb_articles[{i}].path {path!r} escapes package root",
            ) from None
        if not candidate.is_file():
            raise ManifestError(
                f"kb_articles[{i}].path {path!r} not found at {candidate}",
            )
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ManifestError(
                f"kb_articles[{i}].slug must be a non-empty string, "
                f"got {slug!r}",
            )
        if slug.startswith("/") or ".." in slug.split("/"):
            raise ManifestError(
                f"kb_articles[{i}].slug {slug!r} must not be absolute or "
                f"contain '..' components",
            )
        if slug in seen_slugs:
            raise ManifestError(
                f"kb_articles: duplicate slug {slug!r}",
            )
        seen_slugs.add(slug)
        out.append(KbArticleRef(path=path, slug=slug))
    return tuple(out)


def _parse_trigger_subscriptions(
    raw_value: Any,
) -> tuple[SubscriptionRef, ...]:
    items = _ensure_list_of_dict(raw_value, "trigger_subscriptions")
    out: list[SubscriptionRef] = []
    allowed_keys = {"event", "handler"}
    for i, item in enumerate(items):
        unknown = set(item.keys()) - allowed_keys
        if unknown:
            raise ManifestError(
                f"trigger_subscriptions[{i}] has unknown keys: "
                f"{sorted(unknown)}; allowed: {sorted(allowed_keys)}",
            )
        missing = allowed_keys - set(item.keys())
        if missing:
            raise ManifestError(
                f"trigger_subscriptions[{i}] missing required keys: "
                f"{sorted(missing)}",
            )
        event = item.get("event")
        if not isinstance(event, str) or not event.strip():
            raise ManifestError(
                f"trigger_subscriptions[{i}].event must be a non-empty "
                f"string, got {event!r}",
            )
        handler = _check_handler_ref(
            "trigger_subscriptions", item.get("handler"), i, key="handler",
        )
        out.append(SubscriptionRef(event=event, handler=handler))
    return tuple(out)


def _parse_triggers(
    raw_value: Any, *, source_path: Path,
) -> tuple[TriggerRef, ...]:
    """Parse the ``triggers:`` manifest section (PR-B).

    Each entry declares one in-process Trigger instance the package
    contributes.  The referenced ``module`` must exist as a .py file
    within the package directory (under ``triggers/`` by convention).
    Type-specific config validation is the trigger class's job — the
    manifest layer only checks shape and path safety.
    """
    items = _ensure_list_of_dict(raw_value, "triggers")
    out: list[TriggerRef] = []
    seen_names: set[str] = set()
    seen_types: set[str] = set()
    allowed_keys = {"name", "type", "module", "config", "enabled"}
    for i, item in enumerate(items):
        unknown = set(item.keys()) - allowed_keys
        if unknown:
            raise ManifestError(
                f"triggers[{i}] has unknown keys: {sorted(unknown)}; "
                f"allowed: {sorted(allowed_keys)}",
            )
        missing = {"name", "type", "module"} - set(item.keys())
        if missing:
            raise ManifestError(
                f"triggers[{i}] missing required keys: {sorted(missing)}",
            )
        name = _check_ident("triggers", item.get("name"), i, key="name")
        if name in seen_names:
            raise ManifestError(
                f"triggers: duplicate instance name {name!r}",
            )
        seen_names.add(name)
        type_name = item.get("type")
        if (
            not isinstance(type_name, str)
            or not type_name.strip()
            or not re.match(r"^[a-z][a-z0-9_.\-]{0,63}$", type_name)
        ):
            raise ManifestError(
                f"triggers[{i}].type must match "
                f"[a-z][a-z0-9_.\\-]{{0,63}}, got {type_name!r}",
            )
        # Multiple instances of the same trigger type are allowed (a
        # package might want two polling triggers, e.g. one for inbox
        # and one for sent).  We just note the type so the installer
        # knows how many to expect.
        seen_types.add(type_name)
        module_rel = _check_relative_path("triggers", item.get("module"), i)
        candidate = (source_path / module_rel).resolve()
        try:
            candidate.relative_to(source_path.resolve())
        except ValueError:
            raise ManifestError(
                f"triggers[{i}].module {module_rel!r} escapes package root",
            ) from None
        if not candidate.is_file():
            raise ManifestError(
                f"triggers[{i}].module {module_rel!r} not found at "
                f"{candidate}",
            )
        if not candidate.name.endswith(".py"):
            raise ManifestError(
                f"triggers[{i}].module {module_rel!r} must be a .py file",
            )
        config_raw = item.get("config", {})
        if not isinstance(config_raw, dict):
            raise ManifestError(
                f"triggers[{i}].config must be a mapping, got "
                f"{type(config_raw).__name__}",
            )
        enabled_raw = item.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            raise ManifestError(
                f"triggers[{i}].enabled must be a bool, got "
                f"{type(enabled_raw).__name__}",
            )
        out.append(TriggerRef(
            name=name,
            type=type_name,
            module=module_rel,
            config=dict(config_raw),
            enabled=enabled_raw,
        ))
    return tuple(out)


def _parse_allowlist_proposals(
    raw_value: Any,
) -> tuple[AllowlistProposal, ...]:
    """Parse ``allowlist_proposals`` and validate each entry's policy type.

    Each entry must be a mapping with exactly the keys ``type`` and
    ``value``.  The ``type`` must be one of the platform's known policy
    types (the nine reserved names in ``carpenter.security.policies.POLICY_TYPES``);
    unknown types are rejected at parse time as a typo-defense gate
    (a future PR will add a richer ``policies`` field for declaring new
    types).
    """
    items = _ensure_list_of_dict(raw_value, "allowlist_proposals")
    if not items:
        return ()
    # Lazy import: keeps the manifest module's import surface minimal,
    # and keeps tests that stub out the security module from breaking.
    try:
        from ..security.policies import POLICY_TYPES
    except ImportError as exc:  # pragma: no cover — defensive
        raise ManifestError(
            "Cannot validate allowlist_proposals: "
            "carpenter.security.policies unavailable",
        ) from exc

    out: list[AllowlistProposal] = []
    seen: set[tuple[str, str]] = set()
    allowed_keys = {"type", "value"}
    for i, item in enumerate(items):
        unknown = set(item.keys()) - allowed_keys
        if unknown:
            raise ManifestError(
                f"allowlist_proposals[{i}] has unknown keys: "
                f"{sorted(unknown)}; allowed: {sorted(allowed_keys)}",
            )
        missing = allowed_keys - set(item.keys())
        if missing:
            raise ManifestError(
                f"allowlist_proposals[{i}] missing required keys: "
                f"{sorted(missing)}",
            )
        ptype = item.get("type")
        if not isinstance(ptype, str) or not ptype.strip():
            raise ManifestError(
                f"allowlist_proposals[{i}].type must be a non-empty "
                f"string, got {ptype!r}",
            )
        if ptype not in POLICY_TYPES:
            raise ManifestError(
                f"allowlist_proposals[{i}].type {ptype!r} is not a "
                f"recognised policy type.  Known types: "
                f"{sorted(POLICY_TYPES)}.  Packages may not introduce "
                f"new policy types in B-full.",
            )
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(
                f"allowlist_proposals[{i}].value must be a non-empty "
                f"string, got {value!r}",
            )
        key = (ptype, value)
        if key in seen:
            raise ManifestError(
                f"allowlist_proposals: duplicate entry "
                f"(type={ptype!r}, value={value!r})",
            )
        seen.add(key)
        out.append(AllowlistProposal(policy_type=ptype, value=value))
    return tuple(out)


_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# Env-var suffixes (``required_keys`` for ``kind: env``).  UPPER_SNAKE,
# must start with a letter.  No length cap here — the prefix is the
# length-bounded part; suffixes mirror conventional env-var naming.
_ENV_SUFFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _parse_oauth_credential(
    i: int, item: dict[str, Any],
) -> OAuthCredentialRef:
    """Parse a single ``kind: oauth`` credential entry.

    Matches the generic OAuth-callback flow in
    :mod:`carpenter.api.oauth`.  The ``env_key_prefix`` is validated by
    the caller (so the cross-kind uniqueness check can be hoisted); this
    helper validates the OAuth-specific keys.
    """
    allowed_keys = {
        "kind", "provider", "env_key_prefix", "authorize_url",
        "token_url", "scopes",
    }
    unknown = set(item.keys()) - allowed_keys
    if unknown:
        raise ManifestError(
            f"credential_requirements[{i}] (kind=oauth) has unknown keys: "
            f"{sorted(unknown)}; allowed: {sorted(allowed_keys)}",
        )
    missing = allowed_keys - set(item.keys())
    if missing:
        raise ManifestError(
            f"credential_requirements[{i}] (kind=oauth) missing required "
            f"keys: {sorted(missing)}",
        )
    provider = item.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ManifestError(
            f"credential_requirements[{i}].provider must be a non-empty string, "
            f"got {provider!r}",
        )
    for url_key in ("authorize_url", "token_url"):
        url = item.get(url_key)
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ManifestError(
                f"credential_requirements[{i}].{url_key} must be an absolute "
                f"https:// URL, got {url!r}",
            )
    scopes = item.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise ManifestError(
            f"credential_requirements[{i}].scopes must be a non-empty list of "
            f"strings, got {scopes!r}",
        )
    scope_tuple: list[str] = []
    for j, scope in enumerate(scopes):
        if not isinstance(scope, str) or not scope.strip():
            raise ManifestError(
                f"credential_requirements[{i}].scopes[{j}] must be a non-empty "
                f"string, got {scope!r}",
            )
        scope_tuple.append(scope.strip())
    return OAuthCredentialRef(
        kind="oauth",
        provider=provider.strip(),
        env_key_prefix=item["env_key_prefix"],
        authorize_url=item["authorize_url"],
        token_url=item["token_url"],
        scopes=tuple(scope_tuple),
    )


def _parse_env_credential(
    i: int, item: dict[str, Any],
) -> EnvCredentialRef:
    """Parse a single ``kind: env`` credential entry.

    Describes plain env-var credentials (e.g. an IMAP/SMTP app
    password) with no authorization flow.  The ``env_key_prefix`` is
    validated by the caller; this helper validates the env-specific
    keys.
    """
    allowed_keys = {"kind", "provider", "env_key_prefix", "required_keys"}
    unknown = set(item.keys()) - allowed_keys
    if unknown:
        raise ManifestError(
            f"credential_requirements[{i}] (kind=env) has unknown keys: "
            f"{sorted(unknown)}; allowed: {sorted(allowed_keys)}",
        )
    missing = allowed_keys - set(item.keys())
    if missing:
        raise ManifestError(
            f"credential_requirements[{i}] (kind=env) missing required "
            f"keys: {sorted(missing)}",
        )
    provider = item.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ManifestError(
            f"credential_requirements[{i}].provider must be a non-empty string, "
            f"got {provider!r}",
        )
    required_keys = item.get("required_keys")
    if not isinstance(required_keys, list) or not required_keys:
        raise ManifestError(
            f"credential_requirements[{i}].required_keys must be a non-empty "
            f"list of strings, got {required_keys!r}",
        )
    seen_suffixes: set[str] = set()
    key_tuple: list[str] = []
    for j, key in enumerate(required_keys):
        if not isinstance(key, str) or not _ENV_SUFFIX_RE.match(key):
            raise ManifestError(
                f"credential_requirements[{i}].required_keys[{j}] must match "
                f"{_ENV_SUFFIX_RE.pattern!r}, got {key!r}",
            )
        if key in seen_suffixes:
            raise ManifestError(
                f"credential_requirements[{i}].required_keys has duplicate "
                f"entry {key!r}",
            )
        seen_suffixes.add(key)
        key_tuple.append(key)
    return EnvCredentialRef(
        kind="env",
        provider=provider.strip(),
        env_key_prefix=item["env_key_prefix"],
        required_keys=tuple(key_tuple),
    )


def _parse_credentials(
    raw_value: Any,
) -> tuple[OAuthCredentialRef | EnvCredentialRef, ...]:
    """Parse the ``credential_requirements`` field.

    Two ``kind`` flavours are accepted:

    * ``oauth`` — an OAuth 2.0 authorization-code credential matching the
      generic OAuth-callback flow in :mod:`carpenter.api.oauth`.  Yields
      an :class:`OAuthCredentialRef`.
    * ``env`` — plain environment-variable credentials (e.g. an
      IMAP/SMTP mailbox accessed with an app password): a provider tag,
      an ``env_key_prefix``, and a list of ``required_keys`` suffixes.
      Yields an :class:`EnvCredentialRef`.  Install-time ``.env``
      writing and runtime loading are intentionally out of scope here
      (separate future PR); this layer only declares and validates.

    Other kinds are reserved for future PRs and are rejected.

    ``env_key_prefix`` must be unique across ALL entries regardless of
    kind, so mixed manifests cannot collide on the same env namespace.

    The field is named ``credential_requirements`` (NOT
    ``credentials``) because the security validator in
    :mod:`carpenter.packages.security` forbids the bare word
    ``credentials`` as a top-level key — that traditionally meant
    bundled credential *bytes*, which packages may never ship.
    """
    items = _ensure_list_of_dict(raw_value, "credential_requirements")
    if not items:
        return ()
    out: list[OAuthCredentialRef | EnvCredentialRef] = []
    seen_prefixes: set[str] = set()
    for i, item in enumerate(items):
        kind = item.get("kind")
        if kind not in ("oauth", "env"):
            raise ManifestError(
                f"credential_requirements[{i}].kind must be 'oauth' or 'env' "
                f"(got {kind!r}); other kinds are reserved for future PRs",
            )
        # Validate env_key_prefix and its cross-kind uniqueness here so
        # that mixed (oauth + env) manifests are checked together.
        prefix = item.get("env_key_prefix")
        if not isinstance(prefix, str) or not _ENV_PREFIX_RE.match(prefix):
            raise ManifestError(
                f"credential_requirements[{i}].env_key_prefix must match "
                f"{_ENV_PREFIX_RE.pattern!r}, got {prefix!r}",
            )
        if prefix in seen_prefixes:
            raise ManifestError(
                f"credential_requirements: duplicate env_key_prefix {prefix!r}",
            )
        seen_prefixes.add(prefix)
        if kind == "oauth":
            out.append(_parse_oauth_credential(i, item))
        else:
            out.append(_parse_env_credential(i, item))
    return tuple(out)


def _parse_manifest_dict(
    data: dict[str, Any], *, source_path: Path,
) -> PackageManifest:
    if not isinstance(data, dict):
        raise ManifestError(
            f"Manifest at {source_path} must be a YAML mapping, "
            f"got {type(data).__name__}",
        )

    # Reject unknown fields — see module docstring for rationale.
    unknown = set(data.keys()) - _ALLOWED_FIELDS
    if unknown:
        raise ManifestError(
            f"Manifest at {source_path} has unknown fields: "
            f"{sorted(unknown)}.  Allowed fields: {sorted(_ALLOWED_FIELDS)}",
        )

    # Required fields.
    missing = _REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise ManifestError(
            f"Manifest at {source_path} missing required fields: "
            f"{sorted(missing)}",
        )

    name = data["name"]
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ManifestError(
            f"Manifest at {source_path}: 'name' must match "
            f"{_NAME_RE.pattern!r}, got {name!r}",
        )

    version = data["version"]
    if not isinstance(version, (str, int, float)):
        raise ManifestError(
            f"Manifest at {source_path}: 'version' must be a string, "
            f"got {type(version).__name__}",
        )
    version = str(version).strip()
    if not version:
        raise ManifestError(
            f"Manifest at {source_path}: 'version' must be non-empty",
        )

    description = data["description"]
    if not isinstance(description, str) or not description.strip():
        raise ManifestError(
            f"Manifest at {source_path}: 'description' must be a "
            f"non-empty string",
        )

    chat_tools = _ensure_list_of_str(data.get("chat_tools"), "chat_tools")

    kb_namespace = data.get("kb_namespace", name)
    if not isinstance(kb_namespace, str) or not _NAME_RE.match(kb_namespace):
        raise ManifestError(
            f"Manifest at {source_path}: 'kb_namespace' must match "
            f"{_NAME_RE.pattern!r}, got {kb_namespace!r}",
        )

    compat_raw = data.get("platform_compatibility")
    if compat_raw is None:
        platform_compatibility: tuple[str, ...] = ("any",)
    else:
        platform_compatibility = _ensure_list_of_str(
            compat_raw, "platform_compatibility",
        )
        if not platform_compatibility:
            platform_compatibility = ("any",)

    arc_templates = _parse_arc_templates(
        data.get("arc_templates"), source_path=source_path,
    )
    judge_handlers = _parse_judge_handlers(
        data.get("judge_handlers"), source_path=source_path,
    )
    data_models = _parse_data_models(
        data.get("data_models"), source_path=source_path,
    )
    kb_articles = _parse_kb_articles(
        data.get("kb_articles"), source_path=source_path,
    )
    trigger_subscriptions = _parse_trigger_subscriptions(
        data.get("trigger_subscriptions"),
    )
    triggers = _parse_triggers(
        data.get("triggers"), source_path=source_path,
    )
    allowlist_proposals = _parse_allowlist_proposals(
        data.get("allowlist_proposals"),
    )
    credentials = _parse_credentials(data.get("credential_requirements"))

    # Cross-field consistency checks for the new D24 fields.
    declared_kinds = set(data_models)
    declared_judge_names = {h.name for h in judge_handlers}
    for i, t in enumerate(arc_templates):
        for kind_field, kind_value in (
            ("briefing_kind", t.briefing_kind),
            ("extract_kind", t.extract_kind),
        ):
            if kind_value is not None and kind_value not in declared_kinds:
                raise ManifestError(
                    f"arc_templates[{i}].{kind_field}={kind_value!r} is "
                    f"not declared in data_models {sorted(declared_kinds)}",
                )
        if t.judge_handler is not None and declared_judge_names:
            handler_func = t.judge_handler.split(":", 1)[1]
            handler_module = t.judge_handler.split(":", 1)[0]
            referenced_known = (
                handler_func in declared_judge_names
                or any(
                    h.module == handler_module
                    and (h.name == handler_func or True)
                    for h in judge_handlers
                )
            )
            if not referenced_known:
                raise ManifestError(
                    f"arc_templates[{i}].judge_handler "
                    f"{t.judge_handler!r} does not match any declared "
                    f"judge_handlers {sorted(declared_judge_names)}",
                )

    return PackageManifest(
        name=name,
        version=version,
        description=description.strip(),
        chat_tools=chat_tools,
        kb_namespace=kb_namespace,
        platform_compatibility=platform_compatibility,
        arc_templates=arc_templates,
        judge_handlers=judge_handlers,
        data_models=data_models,
        kb_articles=kb_articles,
        trigger_subscriptions=trigger_subscriptions,
        triggers=triggers,
        allowlist_proposals=allowlist_proposals,
        credential_requirements=credentials,
        source_path=source_path,
    )


def load_manifest(manifest_path: Path | str) -> PackageManifest:
    """Load and shape-validate a manifest YAML at ``manifest_path``.

    Args:
        manifest_path: Path to ``manifest.yaml`` (the file itself, not
            its parent directory).

    Returns:
        :class:`PackageManifest` with ``source_path`` set to the
        package directory (parent of the manifest file).

    Raises:
        ManifestError: If the YAML is malformed, fields are missing or
            extraneous, or values fail type/shape checks.
        FileNotFoundError: If the manifest file does not exist.
    """
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Manifest not found: {path}")

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover — yaml is a project dep
        raise ManifestError(
            "PyYAML is required to load capability-package manifests"
        ) from exc

    try:
        with open(path, encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except yaml.YAMLError as exc:
        raise ManifestError(
            f"Malformed YAML in {path}: {exc}",
        ) from exc

    if data is None:
        raise ManifestError(f"Manifest {path} is empty")

    return _parse_manifest_dict(data, source_path=path.parent)
