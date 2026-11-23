Verification Reports
====================

The automated CI pipeline relies on a structured JSON schema for reporting verification results, eliminating brittle log-parsing logic.

Unified Verification Model
--------------------------

All testing suites (e.g., fuzzing, performance, live drift detection) must output a structured JSON file matching the ``VerificationReport`` model defined in ``imednet.models.verification``.

The central model requires:
- ``version``: The schema version (e.g., ``"1.0"``).
- ``track``: The verification track (e.g., ``"fuzzing"``, ``"performance"``, ``"validation"``, ``"live_tests"``, ``"drift"``).
- ``status``: Pass or fail outcome.
- ``violations``: A list of specific errors or rule violations (using ``VerificationViolation``).
- ``metrics``: An optional list of performance metrics (using ``VerificationPerformanceMetric``).
- ``summary``: A human-readable summary.

Extending the Report Schema
---------------------------

If you need to add a new verification type, extend the ``Literal`` for the ``track`` field in ``VerificationReport`` inside ``packages/core/src/imednet/models/verification.py``.

To add new fields or structures specific to your testing, use the standard Pydantic inheritance patterns or add optional fields to the existing models.

Writing Reports in CI
---------------------

Verification suites use ``tests/utils/verification_reporter.py`` (``VerificationReporter``) to emit validated JSON files matching ``reports/verification/*_report.json`` (for example, ``reports/verification/performance_report.json``, ``reports/verification/fuzzing_report.json``, and ``reports/verification/drift_report.json``), even when test assertions fail.

The CI pipeline runs ``python scripts/aggregate_verification.py --report-dir reports/verification --expect-track <track>`` to validate all ``*_report.json`` files against ``VerificationReport``, publish ``drift_detected`` and ``drift_count`` outputs (plus ``reports/verification/drift_report.txt`` when violations occur), and exit ``1`` whenever reports are missing, expected ``--expect-track`` tracks are absent, JSON payloads are malformed, or any report has ``status == "failed"``.
