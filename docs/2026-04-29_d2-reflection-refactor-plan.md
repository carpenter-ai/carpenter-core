# D2 — Reflection-Name Refactor (Plan)

*Investigation + design only. No implementation. Authored 2026-04-29.*

## TL;DR

The synthesis-doc claim — "name coupling is a smell; identity should be `template_id + step_role`" — is **mostly correct**, but the actual coupling in `carpenter-core` today is narrower than the framing suggests. The platform side already has the right shape: the handler registry keys on `(template_name, step_name)` rather than on raw `arcs.name`, and `find_sibling_arc_id()` already uses template `role` with name-fallback. The lingering smell lives in three concrete places:

1. **The handler registry uses step `name`, not step `role`.** Reflection registers `("reflection", "save-reflection", handler)` etc. The step `name` is human-readable and should be presentation-only; `role` is the structural identifier.
2. **A separate name-coupling pattern exists for verification arcs** (`get_arc_name("judge")`, `"documentation"`, `"correctness_check"`). Same shape, same smell, worth fixing in the same PR.
3. **The acceptance stories `s049`/`s050` (in `carpenter-linux`) assert on `arc.name == "daily-reflection"` / `"save-reflection"` / `"reflect"`.** These stories are also stale (they reference the dropped cadence-based trigger and the dropped `reflections` table) and need an independent refresh; the name-coupling fix is a small piece of that.

The proposed refactor is **medium** (300–800 LOC) including the verification-arc cleanup, **small** (<300 LOC) if scoped to reflection alone. There is **no DB migration risk**: the proposal *adds* a `step_role` column on `arcs` with a backfill, leaves `name` in place as a presentation field, and treats the `role` column as advisory at first (with the registry consulting it ahead of `name`).

---

## 1. Current coupling, mapped

### 1a. The handler-registry dispatch (the actual hot path)

`carpenter/core/arcs/dispatch_handler.py:104-115`:

```python
_template_id = arc_info_early.get("template_id")
_step_name = arc_info_early.get("name")
if _template_id and _step_name:
    _template = template_manager.get_template(_template_id)
    if _template:
        _handler = handler_registry.lookup_step_handler(
            _template["name"], _step_name,
        )
        if _handler:
            await _handler(arc_id, arc_info_early)
            return
```

The registry is keyed on `(template_name: str, step_name: str)`. Reflection registers with the human-readable step names:

`config_seed/templates/reflection/__init__.py:33-41`:

```python
registry.register_step_handler("reflection", "gather-activity", handle_gather_activity)
registry.register_step_handler("reflection", "save-reflection", handle_save_reflection)
registry.register_step_handler("reflection", "dispatch-actions", handle_dispatch_actions)
```

**This is the principal smell.** The registry should key on `role`. The `reflection.yaml` template already declares `role:` for every step (`prepare`, `analyze`, `persist`, `dispatch`); only `skill-kb-review.yaml` is missing them.

### 1b. Sibling lookup — already on `role`, with fallback

`carpenter/core/engine/arc_outputs.py:81-153`. `find_sibling_arc_id()` resolves siblings by template-step `role` first, then falls back to `name == role`, then to `arcs.name == role`. This is the desired shape — exactly what the synthesis prescribes for handler dispatch as well. Reflection step handlers already call `find_sibling_arc_id(arc_id, "analyze")` rather than `"reflect"`.

### 1c. Verification arc name dispatch (same smell, different feature)

`carpenter/core/arcs/dispatch_handler.py:93-95, 201`:

```python
if arc_info_early.get("name") == _get_varc_name("judge"):
    await _handle_judge_verification(arc_id, arc_info_early)
    ...
if arc_info_post and arc_info_post.get("name") == _get_varc_name("documentation"):
    ...
```

`carpenter/core/arcs/judge_verification.py:161, 240`:

```python
if sib["name"] == _get_vname("documentation") and sib["status"] == "pending":
```

`carpenter/core/arcs/verification.py` defines four built-in verification arc names (`verify-correctness`, `verify-quality`, `judge-verification`, `post-verification-docs`) configurable under `verification.arc_names` in `config.yaml`. The fact that they are configurable strings is itself an admission that the underlying identity is the role, not the name.

### 1d. `CODING_CHANGE_PREFIX` startswith

`carpenter/core/arcs/__init__.py:15` plus six call sites in `dispatch_handler.py`, `verification.py`, `root_failure_handler.py`, `judge_verification.py` use `arc.name.startswith("coding-change")` to identify coding arcs. This is the same shape but **not** what D2 is about; it could be replaced by a column (`arc_kind` or similar), but that is a separate refactor — coding-change arcs are not template-instantiated and do not have a `step_role`.

### 1e. Acceptance-story side

`/home/pi/repos/carpenter-linux/user_stories/s049_daily_reflection_template.py` and `s050_reflection_save_step.py` assert:

- `arc.get("name") == "daily-reflection"` — **stale**, parent arc is now named `reflection` per `reflection.yaml: action.arc_name`.
- `"reflect" in child_names`, `"save-reflection" in child_names`, `"reflect" / "save-reflection"` lookups by name — these are the name-coupling assertions D2 calls out.
- `cadence="daily"`, `reflections` table — **doubly stale**, reflection is now per-arc-completion (no cadence) and writes to the KB (`reflections/`), not the dropped `reflections` table.

`s052_filesystem_access_restricted.py` does not contain reflection-name coupling (mis-attributed in the brief).

### 1f. Other name-coupling found

- `tests/core/test_reflection_dispatch_actions.py:110-111` looks up arcs by name in a `by_name` dict — test convenience, not platform code; it can either rely on names (test data shape) or shift to roles.

No other production-code dispatch keys off reflection names. The grep surface is clean.

---

## 2. Verifying the assumption

> "Templates define step structure; arcs are agent-strategy units. `template_id + step_role` is the right invariant."

**Verdict: correct as architectural framing, with two caveats.**

**Caveat 1 — `template_name`, not `template_id`.** Numeric `template_id` is a row PK in `workflow_templates` and can shift between reseeds. The handler registry already uses `template_name` (UNIQUE column). The plan must do the same: identity is `(template_name, step_role)`, **not** `(template_id, step_role)`. The synthesis text was loose about this.

**Caveat 2 — `name` is also load-bearing in two places that the simple "name is presentation" framing misses.**

- **`arcs.name` is used as a fallback in `find_sibling_arc_id()`**, which means non-template arcs (the fallback path) and pre-role templates (skill-kb-review currently) still depend on it. The fallback must remain.
- **`CODING_CHANGE_PREFIX` checks** (1d above) treat `name` as a discriminator for arc kind, distinct from step-role identity. These are unaffected by D2 but worth flagging so the refactor doesn't accidentally remove the prefix.

Otherwise the assumption holds. `name` is appropriate as a presentation field once the dispatch path keys on role.

---

## 3. Refactor sketch

### Goal

`if arc.template_name == "reflection" and arc.step_role == "persist":` — or, more precisely, never write that branch at all, and instead let the handler registry route by `(template_name, step_role)`.

### 3a. Schema change

Add one column to `arcs`:

```sql
ALTER TABLE arcs ADD COLUMN step_role TEXT;
CREATE INDEX IF NOT EXISTS idx_arcs_step_role ON arcs(template_id, step_role);
```

`step_role` is `NULL` for non-template arcs and for arcs from templates that haven't declared roles yet. **Backfill**: in the migration, for each existing arc with `template_id IS NOT NULL`, look up the step in `workflow_templates.steps_json` matching `arcs.name`, and write `steps[i].role` into `arcs.step_role` if present. Arcs whose template has no `role` for that step stay `NULL`.

### 3b. Template authoring

Add `role:` to every step in `config_seed/templates/skill-kb-review/skill-kb-review.yaml` (currently has none). Existing roles in `reflection.yaml` are already correct.

The role vocabulary should be small and conventional: `prepare`, `analyze`, `persist`, `dispatch`, `verifier`, `judge`, `docs`, etc. **Roles are not unique within a template** by design (a template could have two `verifier` siblings); when the registry needs to disambiguate, it can fall back to step `name`.

### 3c. `instantiate_template` populates `step_role`

`carpenter/core/engine/template_manager.py:271-280`: pass `step_role=step.get("role")` when calling `arc_manager.create_arc`. `create_arc` accepts and writes the column. No other call site needs to change.

### 3d. Handler registry now keys on role

Two changes:

- `register_step_handler(template_name, step_role, handler)` (rename parameter).
- `dispatch_handler` looks up by `(template_name, arc.step_role)`, falling back to `(template_name, arc.name)` for templates that haven't declared roles yet.

```python
_template_name = _template["name"]
_step_role = arc_info_early.get("step_role")
_step_name = arc_info_early.get("name")
_handler = (
    handler_registry.lookup_step_handler(_template_name, _step_role)
    if _step_role else None
) or handler_registry.lookup_step_handler(_template_name, _step_name)
```

Reflection's `__init__.py` re-registers using roles (`"prepare"`, `"persist"`, `"dispatch"`). The reflect step has no Python handler (it's an LLM step) and so doesn't need to register.

### 3e. Verification arcs (in-scope cleanup)

Replace `arc.name == get_arc_name("judge")` with `arc.step_role == "judge"` (or equivalent role names). For this to work:

- The pseudo-template that the verification subsystem instantiates needs to declare roles. In practice the verification arcs are *not* template-instantiated today — they are created directly by `create_verification_arcs()` in `verification.py`. That code can simply pass `step_role="judge" / "verifier-correctness" / "verifier-quality" / "docs"` to `create_arc`.
- `_get_varc_name("judge")` then becomes a presentation lookup (used only when humans need to read a name); the dispatch decision keys on role.
- `verification.arc_names` config remains for naming flexibility but no longer changes dispatch behavior.

### 3f. `find_sibling_arc_id` simplification

Once arcs carry `step_role` directly, `find_sibling_arc_id` no longer needs to load `workflow_templates.steps_json` to resolve roles — it can read `arcs.step_role` directly. The legacy `arcs.name == role` and `step.name == role` fallbacks stay for backward compatibility with arcs predating the column.

### 3g. Caller side: does anyone "now have to provide step_role"?

No. `step_role` is supplied **only by `instantiate_template`** (from the template YAML) and by the verification subsystem (from a small constant table). All other arc creation (chat-spawned EXECUTOR arcs, coding-change arcs, batch-created REVIEWER/JUDGE arcs from agent code, root reflection arcs spawned by subscriptions) does not need a role and leaves the column NULL. The chat tool surface and `arc.create()` API are unchanged.

### 3h. Backward-compat for existing arcs in the DB

The backfill (3a) covers historical template-instantiated arcs. Arcs whose templates didn't declare a role get `NULL` and continue to dispatch via the name-fallback path. This is fail-soft: nothing breaks during deploy; the role-keyed path activates for arcs created post-migration.

---

## 4. Story-side change

### s049 (`Daily Reflection Template End-to-End`)

Multiple problems beyond name coupling:

- Trigger injection uses `event_type="reflection.trigger"` with `cadence`. **The current platform has no such work item type**; reflection now triggers on `arc.status_changed` for root arcs reaching `completed`. The story must inject a fake completed root arc, not a cadence work item.
- Asserts `parent_arc.name == "daily-reflection"`. New: assert `parent_arc.template_name == "reflection"` (via join through `workflow_templates`).
- Asserts `"reflect" in child_names`, `"save-reflection" in child_names`. New: assert that children with `step_role` `analyze`, `persist`, `prepare`, `dispatch` exist and are correctly ordered.
- Asserts on the dropped `reflections` table. New: assert a KB entry under `reflections/` exists for the reflected arc id.

This is essentially a story rewrite, not a tweak. The name-coupling fix is the smallest piece.

### s050 (`Reflection Save Step Persists Output`)

Same shape: replace the `name == "save-reflection"` lookup with a `step_role == "persist"` lookup. Replace the `reflect` arc lookup similarly with `step_role == "analyze"`. Drop the `reflections` table assertion in favor of a KB-entry check.

### s052

No reflection-name coupling. Out of scope for D2.

### Story helper to add

`DBInspector.get_arc_by_role(parent_id, step_role)` — returns the child whose `step_role` matches. Hides the join through `workflow_templates` and lets new stories read naturally. Existing `get_arc_children` plus a list comprehension on `step_role` works too.

---

## 5. Sizing

**Medium (300–800 LOC)** when including verification-arc cleanup. Breakdown:

| Slice | Est. LOC |
|---|---|
| Migration: add `step_role` column + backfill SQL | 60 |
| `template_manager.instantiate_template` change | 5 |
| `arc_manager.create_arc` accepts `step_role` | 15 |
| Handler registry + dispatch lookup change | 30 |
| Reflection template re-registration (role keys) | 10 |
| `skill-kb-review.yaml` add `role:` to four steps | 10 |
| Verification arcs: pass `step_role`, replace name-eq dispatch | 80 |
| `find_sibling_arc_id` simplification (read column directly) | 30 |
| s049 + s050 rewrite (out-of-tree, in `carpenter-linux`) | 250 |
| Unit tests (registry, dispatch, sibling lookup, migration) | 150 |
| **Total** | **~640** |

**Small (<300 LOC)** if scoped to reflection only and verification cleanup is deferred to its own PR. The verification cleanup is mechanically identical so bundling is reasonable, but it touches security-relevant code paths (verification + judge) — that is itself a reason to consider it as a separate, more carefully-reviewed PR. **Recommendation: split into two PRs**, do reflection first, verification second once the new shape is proven.

---

## 6. Risks

### 6a. DB migration

Low risk. Adding a nullable column is non-destructive. The backfill is a join through `workflow_templates.steps_json` (JSON parse in Python during migration; SQLite has no JSON path operators that we depend on). On the live Pi DB, this is one query plus an in-Python loop over a small number of historical template-instantiated arcs — fast and safe. Roll-forward only; no schema rollback needed.

### 6b. Production arcs in flight at deploy time

Arcs created before the migration will have `step_role IS NULL`. The dispatch path's name-fallback handles this. Arcs created mid-deploy (between schema change and code restart) are unlikely on this single-Pi system; if they happen they look the same as legacy arcs and dispatch via name. **The carpenter daemon on this Pi must be restarted as part of the deploy** (per memory: `systemctl --user restart carpenter`).

### 6c. Tests that match on names

Searched: only `tests/core/test_reflection_dispatch_actions.py` indexes children by `name`, and that's a test convenience over a generated arc tree, not a platform contract. No risk.

### 6d. Chat-agent tools that take names as parameters

Searched for tool handlers that accept `name=` for reflection arcs: none. The chat surface for arcs is `arc.create()`, `arc.list()`, etc., all of which treat `name` as opaque presentation. No tool currently accepts a step-name parameter. No risk.

### 6e. Verification subsystem is security-relevant

Per coding invariants: "Security-relevant changes are human-gated by default." Verification arc dispatch is the boundary between coding-change implementation and judge approval. Even though the refactor preserves behavior (role replaces name as the dispatch key), the change touches `judge_verification.py`, `dispatch_handler.py:_handle_judge_verification`, and `verification.py`. This is **the** argument for splitting verification cleanup into its own PR with full security review. The reflection PR alone is safe to AI-review.

### 6f. Role-name collisions across templates

Roles are only meaningful within a template — two templates can both have `role: persist`. The registry key `(template_name, step_role)` keeps them separate. No risk.

### 6g. Templates without roles

`skill-kb-review` has no `role:` declarations. Either add them as part of the same PR (recommended; trivial) or rely on the name-fallback path indefinitely. The fallback works correctly; there's no hard requirement to migrate every template at once.

### 6h. Stale acceptance stories surface unrelated drift

Touching s049/s050 will surface that they were already broken (cadence-based trigger removed, `reflections` table dropped). The PR scope balloons unless we acknowledge those issues separately. **Recommendation**: file a separate "rewrite stale reflection acceptance stories" task alongside D2; have D2's story changes assume that rewrite is in flight. Alternatively, fold the rewrite into D2 and accept the larger PR — but this turns D2 from "medium" to "large".

---

## Summary recommendation

1. **Reflection PR (small, ~250 LOC platform + tests):** add `step_role` column with backfill, change handler registry keying, re-register reflection handlers by role, simplify `find_sibling_arc_id` to read the column. Add `role:` to `skill-kb-review.yaml`.
2. **Verification PR (small/medium, ~150 LOC, security-gated):** pass `step_role` from `verification.py`, replace `name ==` dispatch with `step_role ==`, keep `verification.arc_names` for presentation only.
3. **Acceptance-story PR (in `carpenter-linux`, ~250 LOC):** rewrite s049/s050 to match the current per-arc reflection trigger and KB-based persistence, asserting on `step_role` rather than name. Track separately from the platform PR.

Each piece is independently mergeable; the dependency is only one-way (story PR after platform PR). Total work: ~640 LOC across three PRs over a few days, low risk, with the only security-sensitive edits isolated in PR 2.
