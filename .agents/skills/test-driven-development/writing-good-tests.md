# Writing Good Tests (`imednet-toolkit`)

Consult this reference whenever writing tests, adding HTTP fakes (`respx`), or introducing test helpers.

## Core Principles

1. **Every test names the production break it catches.**
2. **Every test exercises real behavior through a public seam.**
3. **Mock only at true system boundaries** (external HTTP via `respx`, system clock/randomness, OS boundaries)—never mock your own internal collaborators.

---

## Principle 1: Name the Break & Derive Expectations Independently

Before writing assertions, answer: *what realistic bug makes this test fail, and why is that a real defect rather than an intentional styling choice?*

- **Independent expectations (no tautologies):** Use explicit literals or independent fixtures (`.agents`/`fake_data` helpers), never production builders on both sides of `assert`:

```python
# BAD: Mirror assertion reuses code under test
expected = build_record_filter(study_key="STUDY-1", form_id=12)
assert build_record_filter(study_key="STUDY-1", form_id=12) == expected

# GOOD: Hand-derived literal contract
assert build_record_filter(study_key="STUDY-1", form_id=12) == "studyKey=='STUDY-1';formId==12"
```

- **No change detectors:** Test observable caller behavior (e.g., retry count, status mapping, emitted row), not private constant values or exact internal call ordering.
- **Behavior over text grep:** Run scripts/CLIs cleanly and assert exit codes, stdout/JSON payloads, or side effects rather than scanning source text unless testing a static schema contract.

---

## Principle 2: Test Through Seams, Not Internals

- **Avoid verifying through side channels:** Create via public interface, inspect via public interface.
- **One logical outcome per test:** Split multi-behavior tests (`"and"` tests) so failure pins one behavior.
- **Dependency injection over internal instantiation:** Accept transports/clients so tests inject an in-memory or `respx` transport cleanly rather than patching deep private attributes (`_client`).

```python
# Hard to test: creates external transport inside workflow logic
def sync_subjects(study_key: str) -> int:
    client = ImednetClient.from_env()
    return len(client.subjects.list(study_key))

# Easy to test at the seam: accepts the client/adapter interface
def sync_subjects(client: StudyClient, study_key: str) -> int:
    return len(client.subjects.list(study_key))
```

- **Boundary mocking rules (`respx` & `fake_data`):**
  - Configure strict `respx` routes per endpoint (`GET /studies/{key}/subjects`) mirroring complete real API structures from `imednet.testing.fake_data`, including pagination/rate-limit headers where downstream parsing reads them.
  - Assert distinct branches (`200` empty, `200` paginated, `401`, `429`, malformed JSON) instead of broad catch-all stubs.
  - Keep test-only cleanup helpers inside `tests/` (`conftest.py` / fixtures), never as methods on production classes.

---

## Mutation Check Before Finishing

Mentally apply five mutations to the production code you touched and confirm tests catch each one:
1. Off-by-one or wrong constant (`max_retries`, page size, threshold).
2. Inverted boolean branch (`if` / `else` or guard skip).
3. Dropped side effect or partial model field write.
4. Empty/default return instead of parsed result or raised domain error.
5. Boundary inputs (`""`, `None`, empty list `[]`, duplicate records, HTTP `429`/`5xx`).
