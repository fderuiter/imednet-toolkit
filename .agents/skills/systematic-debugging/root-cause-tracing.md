# Root Cause Tracing

Bugs deep in a call stack (unexpected side effects, bad working directories, leaked globals) look tempting to guard at the throw site, but symptom checks hide caller bugs. Trace backward to the original invalid producer and fix there.

## Process

1. Capture the immediate failure and full traceback (`pytest -vv --tb=long`).
2. Inspect what immediate helper ran (`Path.cwd()`, HTTP client, parser) and what bad argument reached it.
3. Walk backward caller-by-caller up the stack until you reach where `None`, `""`, or unvalidated state originated.
4. Add temporary targeted tracing right before the dangerous call if unclear (`sys.stderr.write("[DEBUG-...] ...")` + `traceback.print_stack()`), run once (`pytest tests/unit/test_target.py -s`), and strip `[DEBUG-...]` afterward.
5. Fix at the source and pair with `defense-in-depth.md` guards down the call chain.

## Bisecting Test Pollution

If state/files appear mysteriously during full `pytest` runs, locate the offending test file with:

```bash
bash .agents/skills/systematic-debugging/find-polluter.sh '.coverage.tmp' 'tests/unit/test_*.py' 'pytest -q'
```
