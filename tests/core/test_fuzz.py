import contextlib
import json
import logging
import os
import sys
import time
from io import BytesIO
from multiprocessing import Process
from pathlib import Path

import pytest

from tests.utils.verification_reporter import VerificationReporter

atheris = pytest.importorskip("atheris")

pytestmark = pytest.mark.fuzzing

ROOT_DIR = Path(__file__).resolve().parents[2]
SEED_CORPUS_DIR = Path(__file__).resolve().parent / "corpora" / "fuzz_seeds"
DEFAULT_CACHE_ROOT = ROOT_DIR / ".cache" / "verification"
DEFAULT_FUZZ_RUNS = 25000


def _resolve_fuzz_runs() -> int:
    raw = os.environ.get("IMEDNET_FUZZ_RUNS", str(DEFAULT_FUZZ_RUNS)).strip()
    runs = int(raw)
    if runs < 1:
        raise ValueError(f"IMEDNET_FUZZ_RUNS must be >= 1, got {runs}")
    return runs


def run_fuzzer(
    runs: int,
    corpus_dir: str,
    seed_dir: str,
    artifact_dir: str,
    postman_path: str,
) -> None:
    import atheris

    # Suppress logging inside the fuzzer child process without mutating pytest collection state
    logging.disable(logging.CRITICAL)

    with atheris.instrument_imports(include=["imednet", "imednet_sinks"]):
        import imednet
        import imednet.models.engine
        from imednet.datadict import load_from_csv
        from imednet.models.engine import get_contract
        from imednet_sinks.warehouse import _records_to_arrow_table

    os.environ["IMEDNET_POSTMAN_PATH"] = postman_path

    def fuzz_dynamic_model(data: bytes) -> None:
        with open(postman_path, "wb") as f:
            f.write(data)

        imednet.models.engine._CONTRACT_CACHE = None

        with contextlib.suppress(Exception):
            get_contract()

    def fuzz_data_dictionary(data: bytes) -> None:
        with contextlib.suppress(Exception):
            load_from_csv(BytesIO(data))

    def fuzz_warehouse_transformation(data: bytes) -> None:
        try:
            text = data.decode("utf-8", errors="ignore")
            if not text:
                return

            try:
                records_data = json.loads(text)
            except json.JSONDecodeError:
                records_data = [{"record_data": {"field": text[:50]}}]

            if not isinstance(records_data, list):
                records_data = [records_data]

            class DummyRecord:
                def __init__(self, d):
                    self.record_id = str(d.get("id", "1"))
                    self.form_id = str(d.get("form_id", "1"))
                    self.visit_id = str(d.get("visit_id", "1"))
                    self.subject_key = str(d.get("subject_key", "1"))
                    self.record_data = d.get("data", {})

            records = []
            for rd in records_data:
                if isinstance(rd, dict):
                    records.append(DummyRecord(rd))

            _records_to_arrow_table(records)

        except Exception:
            pass

    def TestOneInput(data: bytes) -> None:
        if len(data) < 1:
            return

        mode = data[0] % 3
        if mode == 0:
            fuzz_dynamic_model(data)
        elif mode == 1:
            fuzz_data_dictionary(data)
        elif mode == 2:
            fuzz_warehouse_transformation(data)

    fuzzer_args = [
        sys.argv[0],
        f"-runs={runs}",
        f"-artifact_prefix={artifact_dir.rstrip('/')}/",
        corpus_dir,
        seed_dir,
    ]
    atheris.Setup(fuzzer_args, TestOneInput)
    atheris.Fuzz()


def test_fuzzing() -> None:
    """Fuzz the core models and data pipelines using Atheris."""
    runs = _resolve_fuzz_runs()
    cache_root = Path(os.environ.get("IMEDNET_VERIFICATION_CACHE_DIR", DEFAULT_CACHE_ROOT))
    corpus_dir = cache_root / "fuzz_corpus"
    artifact_dir = cache_root / "fuzz_artifacts"
    postman_path = cache_root / "fuzz_postman.json"

    corpus_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    assert SEED_CORPUS_DIR.is_dir(), f"Missing committed fuzz seed directory: {SEED_CORPUS_DIR}"
    seed_files = [p for p in SEED_CORPUS_DIR.iterdir() if p.is_file()]
    assert seed_files, f"No committed fuzz seed files found in {SEED_CORPUS_DIR}"

    with VerificationReporter(
        "fuzzing",
        passed_summary=f"Completed {runs:,} coverage-guided fuzzing iterations across {len(seed_files)} committed seeds.",
        failed_summary=f"Coverage-guided fuzzing failed during {runs:,}-iteration run.",
    ) as reporter:
        start = time.perf_counter()
        p = Process(
            target=run_fuzzer,
            args=(
                runs,
                str(corpus_dir),
                str(SEED_CORPUS_DIR),
                str(artifact_dir),
                str(postman_path),
            ),
        )
        p.start()
        p.join()
        elapsed = time.perf_counter() - start

        if postman_path.exists():
            postman_path.unlink()

        reporter.add_metric("fuzz_iterations", float(runs), unit="runs")
        reporter.add_metric("fuzz_duration", elapsed, unit="seconds")
        reporter.add_metric("seed_corpus_files", float(len(seed_files)), unit="count")

        if p.exitcode != 0:
            artifacts = sorted(x.name for x in artifact_dir.iterdir() if x.is_file())
            detail = f" (artifacts: {', '.join(artifacts)})" if artifacts else ""
            msg = f"Fuzzer process exited with code {p.exitcode} after {elapsed:.2f}s{detail}"
            reporter.add_violation("fuzzer_crash", msg)
            assert p.exitcode == 0, msg
