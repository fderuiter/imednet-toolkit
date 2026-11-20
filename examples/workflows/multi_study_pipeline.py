"""Multi-Study Parallel Pipeline Example.

======================================
Demonstrates using MultiStudyOrchestrator to run a data extraction pipeline
concurrently across all active iMednet studies, with per-study fault isolation
and structured telemetry context.

Usage::

    IMEDNET_API_KEY=<key> IMEDNET_SECURITY_KEY=<key> python multi_study_pipeline.py

Optional environment variables:
    IMEDNET_WHITELIST   Comma-separated list of study keys to include (e.g., "PROT-01,PROT-02")
    IMEDNET_BLACKLIST   Comma-separated list of study keys to exclude
    IMEDNET_MAX_WORKERS Number of concurrent workers (default: 4)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from imednet import ImednetSDK, MultiStudyOrchestrator
from imednet.utils.json_logging import configure_json_logging

configure_json_logging(level=logging.INFO)
logger = logging.getLogger(__name__)


def extract_study_data(
    study_key: str,
    sdk: ImednetSDK,
    study_logger: Any,
    **kwargs: Any,
) -> dict:
    """Pipeline function: extract subject and record counts for a study.

    This function conforms to the StudyWorkerCallable protocol. It is called
    concurrently for each study resolved by the orchestrator.
    """
    study_logger.info("Starting data extraction")

    with sdk.study_context(study_key):
        subjects = sdk.subjects.list(study_key=study_key)
        records = sdk.records.list(study_key=study_key)

    result = {
        "subject_count": len(list(subjects)),
        "record_count": len(list(records)),
    }
    study_logger.info("Extraction complete: %s", result)
    return result


def main() -> int:
    """Run the multi-study extraction pipeline and return a process exit code."""
    whitelist_raw = os.getenv("IMEDNET_WHITELIST", "")
    blacklist_raw = os.getenv("IMEDNET_BLACKLIST", "")
    max_workers = int(os.getenv("IMEDNET_MAX_WORKERS", "4"))

    whitelist = set(whitelist_raw.split(",")) - {""} if whitelist_raw else None
    blacklist = set(blacklist_raw.split(",")) - {""} if blacklist_raw else None

    with ImednetSDK() as sdk:
        orchestrator = MultiStudyOrchestrator(sdk, max_workers=max_workers)
        results = orchestrator.execute_pipeline(
            extract_study_data,
            whitelist=whitelist,
            blacklist=blacklist,
        )

    success_count = sum(1 for r in results.values() if r["status"] == "SUCCESS")
    failure_count = sum(1 for r in results.values() if r["status"] == "FAILED")

    logger.info(
        "Pipeline complete: %d succeeded, %d failed across %d studies.",
        success_count,
        failure_count,
        len(results),
    )
    print(
        f"Pipeline complete: {success_count} succeeded, {failure_count} failed "
        f"across {len(results)} studies."
    )

    for study_key, result in sorted(results.items()):
        status = result["status"]
        duration = result["duration_seconds"]
        if status == "SUCCESS":
            logger.info("[%s] SUCCESS in %.2fs: %s", study_key, duration, result["data"])
        else:
            logger.error("[%s] FAILED in %.2fs: %s", study_key, duration, result["error"])

    return 0 if failure_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
