"""Aggregates and formats verification JSON reports."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import get_args

from imednet.models.verification import VerificationReport

DEFAULT_REPORT_DIR = Path("reports") / "verification"
VALID_TRACKS = tuple(get_args(VerificationReport.model_fields["track"].annotation))


def _write_outputs(has_drift: bool, drift_count: int) -> None:
    """Publish explicit results, including when no report files were produced."""
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"drift_detected={'true' if has_drift else 'false'}\n")
            f.write(f"drift_count={drift_count}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate and validate VerificationReport files.")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help="Directory containing *_report.json files (default: reports/verification).",
    )
    parser.add_argument(
        "--expect-track",
        action="append",
        default=[],
        choices=VALID_TRACKS,
        help="Require at least one valid report for the given track (repeatable).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run aggregation and exit non-zero on missing, malformed, or failed reports."""
    args = _build_parser().parse_args([] if argv is None else list(argv))
    report_dir: Path = args.report_dir
    expected_tracks: list[str] = list(dict.fromkeys(args.expect_track))

    report_files = sorted(report_dir.glob("*_report.json")) if report_dir.exists() else []
    if not report_files:
        _write_outputs(False, 0)
        if expected_tracks:
            print(
                f"::error::No verification reports found in {report_dir}; "
                f"missing expected track(s): {', '.join(expected_tracks)}"
            )
        else:
            print(f"::error::No verification reports found in {report_dir}.")
        sys.exit(1)

    all_reports: list[VerificationReport] = []
    has_malformed = False

    for rf in report_files:
        with open(rf, encoding="utf-8") as f:
            try:
                data = json.load(f)
                all_reports.append(VerificationReport.model_validate(data))
            except Exception as e:
                print(f"::error::Failed to load malformed report {rf}: {e}")
                has_malformed = True

    if has_malformed:
        print("::error::Invalid or malformed verification reports detected.")
        sys.exit(1)

    has_drift = False
    drift_violations: list[str] = []
    seen_tracks = {r.track for r in all_reports}

    for r in all_reports:
        if r.status == "failed":
            has_drift = True
            for v in r.violations:
                drift_violations.append(f"- {v.violation_type} in track `{r.track}`: {v.message}")

    drift_count = len(drift_violations)
    missing_tracks = [track for track in expected_tracks if track not in seen_tracks]

    _write_outputs(has_drift, drift_count)

    if has_drift:
        report_dir.mkdir(parents=True, exist_ok=True)
        with open(report_dir / "drift_report.txt", "w", encoding="utf-8") as f:
            f.writelines(f"{dv}\n" for dv in drift_violations)
        print(f"::error::Detected {drift_count} verification violations.")

    if missing_tracks:
        print(f"::error::Missing expected verification track(s): {', '.join(missing_tracks)}")

    if has_drift or missing_tracks:
        sys.exit(1)

    print("All verifications passed.")


if __name__ == "__main__":
    main(sys.argv[1:])
