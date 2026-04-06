# Template Rigidity

Template-mandated arcs in Carpenter are **completely immutable** to preserve workflow integrity.

## Overview

When a workflow template is instantiated (via `template_manager.instantiate_template()`), it creates a series of child arcs under a parent arc. Each of these child arcs has:
- `from_template = True`
- `template_id` set to the originating template

These arcs form a **rigid sequence** that cannot be modified.

## Immutability Rules

Template-created arcs (`from_template=True`) have the following restrictions:

### 1. Cannot Have Children Added

```python
# This will raise ValueError
arc_manager.add_child(template_arc_id, "new-child", goal="...")
# Error: Cannot add child to arc N created by template (from_template=True)
```

**Rationale**: Template steps define a specific workflow structure. Allowing arbitrary children would break the template's intended design.

### 2. Cannot Be Deleted

Template arcs should not be manually deleted from the database. The `validate_template_rigidity()` function checks that all template steps remain intact.

### 3. Cannot Be Reordered

The `step_order` of template arcs is fixed by the template definition and should not be modified.

### 4. Cannot Be Modified

While status transitions are allowed (as part of normal workflow execution), the structural properties (name, goal, template_id, from_template flag) should remain unchanged.

## What IS Allowed

- **Status transitions**: Template arcs follow the normal arc lifecycle (pending → active → waiting/completed/failed/cancelled)
- **History entries**: Arcs can have history entries appended
- **Cancellation**: Template arcs can be cancelled (which cascades to descendants)
- **Adding children to the parent**: Non-template children can be added to the parent arc that contains the template steps (but not to the template steps themselves)

## Implementation

The restriction is enforced at the `arc_manager.add_child()` level:

```python
def add_child(parent_id: int, name: str, goal: str | None = None, **kwargs) -> int:
    """Add a child arc to a parent.

    Raises:
        ValueError: If parent was created by a template (from_template=True).
    """
    db = get_db()
    try:
        parent = db.execute(
            "SELECT id, status, from_template FROM arcs WHERE id = ?", (parent_id,)
        ).fetchone()

        if parent["from_template"]:
            raise ValueError(
                f"Cannot add child to arc {parent_id} created by template (from_template=True)"
            )
        # ... rest of implementation
```

## Validation

The `template_manager.validate_template_rigidity(parent_arc_id)` function verifies:
1. The parent has a template_id
2. All template steps exist as child arcs with from_template=True
3. The count matches the template definition
4. The step_orders match the template definition

## Example Template Structure

```
parent-arc (template_id=1)
├── step-1 (from_template=True, step_order=1) ← IMMUTABLE
├── step-2 (from_template=True, step_order=2) ← IMMUTABLE
└── step-3 (from_template=True, step_order=3) ← IMMUTABLE
```

Attempting to add children to step-1, step-2, or step-3 will fail.

However, adding a non-template child to parent-arc is allowed:

```
parent-arc (template_id=1)
├── step-1 (from_template=True, step_order=1)
├── step-2 (from_template=True, step_order=2)
├── step-3 (from_template=True, step_order=3)
└── custom-child (from_template=False, step_order=4) ← ALLOWED
```

## Workflow Templates

Several templates ship with Carpenter:

| Template | Steps | Purpose |
|----------|-------|---------|
| `coding-change` | 3 | Agent writes code in isolated workspace, reviewed before merging |
| `writing-repo-change` | 6 | Git branch, changes, PR, review, approval, merge |
| `dark-factory` | 4 | Autonomous spec-driven development with iterative validation |

All steps in these templates are immutable once instantiated.

## Testing

Template rigidity is tested in:
- `tests/core/test_template_manager.py::test_cannot_add_child_to_template_created_arc`
- `tests/core/test_template_manager.py::test_can_add_child_to_non_template_arc`
- `tests/core/test_template_manager.py::test_validate_template_rigidity_*`

## See Also

- `design.md` — Authoritative system design document
- `carpenter/core/template_manager.py` — Template loading and instantiation
- `carpenter/core/arc_manager.py` — Arc CRUD operations and immutability enforcement
