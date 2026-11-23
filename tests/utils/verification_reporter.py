"""Shared helper for emitting validated VerificationReport JSON files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

from imednet.models.verification import (
    VerificationPerformanceMetric,
    VerificationReport,
    VerificationViolation,
)

TrackName = Literal["fuzzing", "performance", "validation", "live_tests", "drift"]

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_DIR = ROOT_DIR / "reports" / "verification"


def resolve_report_dir(report_dir: Path | str | None = None) -> Path:
    """Resolve the output directory for verification reports."""
    if report_dir is not None:
        return Path(report_dir)
    env_dir = os.environ.get("IMEDNET_VERIFICATION_REPORT_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_REPORT_DIR


def write_verification_report(
    *,
    track: TrackName,
    status: Literal["passed", "failed"],
    summary: str,
    violations: list[VerificationViolation] | None = None,
    metrics: list[VerificationPerformanceMetric] | None = None,
    report_dir: Path | str | None = None,
    filename: str | None = None,
) -> Path:
    """Validate and atomically write a VerificationReport JSON file."""
    report = VerificationReport(
        version="1.0",
        track=track,
        status=status,
        violations=list(violations or []),
        metrics=list(metrics or []),
        summary=summary,
    )
    target_dir = resolve_report_dir(report_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / (filename or f"{track}_report.json")

    payload = report.model_dump_json(indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_path.stem}_",
        suffix=".tmp",
        dir=str(target_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(payload)
        os.replace(tmp_name, target_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    return target_path


class VerificationReporter:
    """Context manager that guarantees VerificationReport emission even on test failure."""

    def __init__(
        self,
        track: TrackName,
        *,
        report_dir: Path | str | None = None,
        filename: str | None = None,
        passed_summary: str | None = None,
        failed_summary: str | None = None,
    ) -> None:
        self.track: TrackName = track
        self.report_dir = report_dir
        self.filename = filename
        self.passed_summary = passed_summary or f"{track} verification passed."
        self.failed_summary = failed_summary or f"{track} verification failed."
        self.violations: list[VerificationViolation] = []
        self.metrics: list[VerificationPerformanceMetric] = []
        self._explicit_summary: str | None = None
        self._failed = False
        self.last_report_path: Path | None = None

    def add_violation(self, violation_type: str, message: str) -> None:
        """Record a structured violation and mark the report as failed."""
        self.violations.append(
            VerificationViolation(violation_type=violation_type, message=message)
        )
        self._failed = True

    def add_metric(self, name: str, value: float, unit: str | None = None) -> None:
        """Record a numeric performance or verification metric."""
        self.metrics.append(VerificationPerformanceMetric(name=name, value=float(value), unit=unit))

    def set_summary(self, summary: str) -> None:
        """Override the report summary text."""
        self._explicit_summary = summary

    def mark_failed(self, summary: str | None = None) -> None:
        """Explicitly mark the verification report status as failed."""
        self._failed = True
        if summary is not None:
            self._explicit_summary = summary

    @property
    def status(self) -> Literal["passed", "failed"]:
        """Current computed report status."""
        return "failed" if (self._failed or bool(self.violations)) else "passed"

    def write(self) -> Path:
        """Validate and write the current VerificationReport to disk."""
        computed_status = self.status
        summary = self._explicit_summary or (
            self.failed_summary if computed_status == "failed" else self.passed_summary
        )
        self.last_report_path = write_verification_report(
            track=self.track,
            status=computed_status,
            summary=summary,
            violations=self.violations,
            metrics=self.metrics,
            report_dir=self.report_dir,
            filename=self.filename,
        )
        return self.last_report_path

    def __enter__(self) -> VerificationReporter:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> Literal[False]:
        if exc_type is not None:
            if exc_type.__name__ == "Skipped" or issubclass(exc_type, KeyboardInterrupt):
                return False
            self._failed = True
            if not self.violations:
                msg = str(exc_val).strip() if exc_val is not None else ""
                self.add_violation(exc_type.__name__, msg or exc_type.__name__)
        self.write()
        return False
