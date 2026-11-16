# Defense-in-Depth Validation

When fixing a bug caused by invalid data or path state, validate at every layer the value crosses so future refactors or direct callers cannot bypass a single guard.

## The Four Layers

1. **Entry point validation:** Reject empty, missing, or malformed inputs at the public interface (`ValueError`, Pydantic model validation).
2. **Domain / workflow validation:** Enforce invariants (`Study`, `Job`, `Record` state prerequisites) before mutating state or building HTTP payloads.
3. **Environment guards:** Refuse unsafe side effects in test/dev contexts (e.g., refusing file mutations outside `tmp_path` during tests, blocking placeholder credentials).
4. **Structured forensic logging:** Log key state and caller context at the danger point (`logger.debug(...)` with sanitized secrets) when prior layers trigger.

```python
from pathlib import Path


def ensure_safe_worktree_dir(directory: Path, *, allowed_root: Path) -> Path:
    """Validate entry existence and confine mutations under *allowed_root*."""
    resolved = directory.resolve()
    root_resolved = allowed_root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"Refusing path outside workspace root: {resolved}")
    return resolved
```
