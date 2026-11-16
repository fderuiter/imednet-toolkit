---
name: systematic-debugging
description: Use when encountering any bug, test failure, flake, slowness, build error, or unexpected behavior before proposing fixes
---

# Systematic Debugging

Find the root cause before attempting any fix. Symptom patches are failure.

```text
NO FIXES WITHOUT A TIGHT REPRO AND ROOT CAUSE INVESTIGATION FIRST
```

## Security & Context Rules

- **Redact secrets first:** Replace API keys, security keys, tokens, or auth headers with `<REDACTED>` before quoting commands, tracebacks, or captured payloads. Keep secrets only in environment variables (`scripts/env.ps1` / `.env`).
- **Domain grounding:** Read `CONTEXT.md` and relevant `docs/adr/` entries when exploring unfamiliar modules so hypotheses and test names match repo vocabulary.

---

## Phase 1: Tight Red Loop & Evidence Gathering

Build a **tight**, deterministic, fast pass/fail command that goes red on *this exact symptom* before inspecting code to theorize:

1. **Choose the fastest signal:** failing `pytest` case at the broken seam, CLI snapshot run, `git bisect run`, traced HTTP replay (`respx` fixture), property/fuzz loop, or headless browser probe. (Last resort for manual steps: copy and adapt `scripts/hitl-loop.template.sh` run via `bash`.)
2. **Tighten & minimise:** Remove non-load-bearing inputs, config, and steps one at a time until removing any remaining element makes the failure disappear. Record the exact command and redacted stderr/traceback.
3. **Inspect changes & multi-layer flow:** Check `git diff`, trace values backward across boundaries (`root-cause-tracing.md`), or inspect condition-based timing (`condition-based-waiting.md`) rather than guessing sleeps.
4. **If you cannot reproduce or observe:** Stop, list what was tried, and ask the user for missing logs (redacted) or environment access. Never invent a hypothesis without a red signal.

---

## Phase 2: Pattern Analysis

Compare broken execution against working sibling code (`packages/core`, `packages/plugins-workflows`, `packages/providers-airflow`, `packages/plugins-sinks`): list concrete differences in state, imports, validation, config, or async lifecycles.

---

## Phase 3: Ranked Falsifiable Hypotheses

Before editing code or tossing in logs:

1. Form **3–5 ranked falsifiable hypotheses**, each stating a testable prediction:
   > `If <X> is the cause, changing <Y> eliminates the failure (or altering <Z> worsens it).`
2. Briefly share the ranked list with your human partner if interactive (or proceed cleanly if running unattended) and test **one variable at a time**.
3. If instrumenting code temporarily, prefix every debug log/print with a unique tag like `[DEBUG-a4f2]` so cleanup is a single `git grep DEBUG-` check. (For performance regressions, benchmark/profile before editing.)

---

## Phase 4: Fix at Source, Regression Test & Architectural Escalation

1. **Verify seam & write failing regression test first** (`test-driven-development`): target the seam where the caller triggers the bug. If no clean seam exists to pin the bug down, flag the module for architectural deepening (`improve-codebase-architecture`) instead of mocking internals.
2. **Apply one minimal fix** at the root cause; add layered boundary validation (`defense-in-depth.md`) where invalid inputs crossed multiple tiers.
3. **Check the 3-fix stop rule:**
   - **If < 3 attempts failed:** return to Phase 1 with the new evidence.
   - **If >= 3 fixes failed:** STOP. Do **not** attempt Fix #4; the repeated failure points to broken shared state or an architectural mismatch. Present findings and discuss structure with your human partner.
4. **Cleanup & Verification Checklist:**
   - Remove every `[DEBUG-...]` log (`git grep DEBUG-` returns nothing) and delete any throwaway repro scripts.
   - Re-run the Phase 1 repro loop and confirm the symptom is gone.
   - Run the full repository test/verification gates (`verification-before-completion`).
   - Summarize the confirmed root cause in your commit / PR message.

## Supporting Techniques (`systematic-debugging/`)

- `root-cause-tracing.md`: backward call-stack tracing + `scripts/find-polluter.sh` (`pytest` test-pollution bisection).
- `defense-in-depth.md`: four-layer boundary validation pattern in Python.
- `condition-based-waiting.md`: polling helper pattern replacing arbitrary `time.sleep()` / `asyncio.sleep()` delays.
