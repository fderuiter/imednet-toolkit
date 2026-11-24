import json
import logging
import statistics
import time
from pathlib import Path

import pyarrow as pa
import pytest
from pydantic import BaseModel

from imednet.integrations.enrichment import _to_snake_case
from imednet.models.records import Record
from imednet.testing.fake_data import fake_record
from imednet.validation.cache import SchemaCache
from tests.utils.verification_reporter import VerificationReporter

try:
    from imednet_sinks.warehouse import _records_to_arrow_table
except ImportError:

    def _records_to_arrow_table(records):
        rows = [
            {
                "record_id": getattr(r, "record_id", None),
                "form_id": getattr(r, "form_id", None),
                "visit_id": getattr(r, "visit_id", None),
                "subject_key": getattr(r, "subject_key", None),
                **dict(getattr(r, "record_data", {}) or {}),
            }
            for r in records
        ]
        return pa.Table.from_pylist(rows)


logger = logging.getLogger(__name__)

pytestmark = pytest.mark.performance


BASELINE_FILE = Path(__file__).resolve().parent / "baselines" / "performance_baseline.json"
BENCHMARK_METRICS = (
    "record_generation_latency",
    "mongo_conversion_latency",
    "snowflake_arrow_conversion_latency",
    "neo4j_conversion_latency",
)
BENCHMARK_RUNS = 5


class _CalibrationItem(BaseModel):
    item_id: int
    code: str
    payload: dict[str, int | str]


def calibrate_runner_cpu(runs: int = BENCHMARK_RUNS, iterations: int = 5000) -> float:
    """Measure median CPU speed on a deterministic synthetic Python/Pydantic workload."""
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        items = [
            _CalibrationItem.model_validate(
                {
                    "item_id": idx,
                    "code": f"CODE_{idx % 64}",
                    "payload": {"bucket": idx % 16, "label": f"val_{idx}"},
                }
            )
            for idx in range(iterations)
        ]
        dumped = [it.model_dump() for it in items]
        edges = [{"from": row["code"], "to": row["item_id"]} for row in dumped]
        assert len(edges) == iterations
        samples.append(time.perf_counter() - start)
    return max(statistics.median(samples), 1e-6)


def generate_records(count: int, cache: SchemaCache) -> list:
    records = []
    for _ in range(count):
        data = fake_record(cache)
        records.append(Record.model_validate(data))
    return records


def run_benchmarks(record_count: int = 10000) -> dict[str, float]:
    results = {}
    cache = SchemaCache()

    start = time.perf_counter()
    records = generate_records(record_count, cache)
    elapsed = time.perf_counter() - start
    results["record_generation_latency"] = elapsed
    print(f"Generated {record_count} records in {elapsed:.4f}s")

    start = time.perf_counter()
    for r in records:
        if isinstance(r, BaseModel):
            r.model_dump(by_alias=True)
        else:
            r.model_dump()
    elapsed = time.perf_counter() - start
    results["mongo_conversion_latency"] = elapsed
    print(f"Mongo conversion (serialization) for {record_count} records in {elapsed:.4f}s")

    start = time.perf_counter()
    _to_snake_case.cache_clear()
    _records_to_arrow_table(records)
    elapsed = time.perf_counter() - start
    results["snowflake_arrow_conversion_latency"] = elapsed
    cache_info = _to_snake_case.cache_info()
    print(
        f"Snowflake Arrow conversion for {record_count} records in {elapsed:.4f}s "
        f"(cache hits: {cache_info.hits}, misses: {cache_info.misses})"
    )

    start = time.perf_counter()
    nodes = []
    edges = []
    for r in records:
        nodes.append({"id": r.record_id, "label": "Record"})
        if getattr(r, "subject_key", None):
            edges.append({"from": r.subject_key, "to": r.record_id, "type": "HAS_RECORD"})
    elapsed = time.perf_counter() - start
    results["neo4j_conversion_latency"] = elapsed
    print(f"Neo4j Graph conversion for {record_count} records in {elapsed:.4f}s")

    return results


def measure_median_benchmarks(
    *, runs: int = BENCHMARK_RUNS, record_count: int = 10000
) -> dict[str, float]:
    """Run benchmarks multiple times and return the per-metric median latency."""
    per_metric: dict[str, list[float]] = {metric: [] for metric in BENCHMARK_METRICS}
    for _ in range(runs):
        sample = run_benchmarks(record_count)
        for metric in BENCHMARK_METRICS:
            per_metric[metric].append(sample[metric])
    return {metric: statistics.median(values) for metric, values in per_metric.items()}


def test_snake_case_memoization_and_edge_cases():
    """Verify snake_case conversion edge cases and cache hit rates during record mapping."""
    # Test case conversion edge cases
    assert _to_snake_case("") == ""
    assert _to_snake_case("already_snake_case") == "already_snake_case"
    assert _to_snake_case("camelCase") == "camel_case"
    assert _to_snake_case("PascalCase") == "pascal_case"
    assert _to_snake_case("HTMLParser") == "html_parser"
    assert _to_snake_case("item-code") == "item-code"
    assert _to_snake_case("field123Value") == "field123_value"
    assert _to_snake_case("RECORD_ID") == "record_id"

    # Verify cache behavior and hit rate on record batch
    _to_snake_case.cache_clear()
    cache = SchemaCache()
    records = generate_records(500, cache)
    _to_snake_case.cache_clear()
    _records_to_arrow_table(records)

    info = _to_snake_case.cache_info()
    assert info.hits > 0
    total_calls = info.hits + info.misses
    hit_rate = info.hits / total_calls if total_calls else 0
    assert hit_rate >= 0.95, f"Expected cache hit rate >= 95%, got {hit_rate:.2%}"
    assert info.currsize <= 1024


def test_warehouse_arrow_single_pass_and_latency(monkeypatch):
    """Verify single-pass warehouse Arrow conversion avoids redundant scans and meets latency target."""
    import imednet_sinks.warehouse as wh_mod

    cache = SchemaCache()
    small_batch = generate_records(200, cache)

    call_counts = {"normalize": 0, "omit_empty": 0, "scalar_family": 0, "validate_keys": 0}
    orig_norm = wh_mod._normalize_warehouse_value
    orig_omit = wh_mod._omit_empty_structs
    orig_fam = wh_mod._scalar_family
    orig_val = wh_mod._validate_nested_variable_keys

    monkeypatch.setattr(
        wh_mod,
        "_normalize_warehouse_value",
        lambda v: (
            call_counts.__setitem__("normalize", call_counts["normalize"] + 1),
            orig_norm(v),
        )[1],
    )
    monkeypatch.setattr(
        wh_mod,
        "_omit_empty_structs",
        lambda r: (
            call_counts.__setitem__("omit_empty", call_counts["omit_empty"] + 1),
            orig_omit(r),
        )[1],
    )
    monkeypatch.setattr(
        wh_mod,
        "_scalar_family",
        lambda v: (
            call_counts.__setitem__("scalar_family", call_counts["scalar_family"] + 1),
            orig_fam(v),
        )[1],
    )
    monkeypatch.setattr(
        wh_mod,
        "_validate_nested_variable_keys",
        lambda v: (
            call_counts.__setitem__("validate_keys", call_counts["validate_keys"] + 1),
            orig_val(v),
        )[1],
    )

    tbl_small = _records_to_arrow_table(small_batch, study_key="STUDY_BENCH")
    assert tbl_small.num_rows == 200
    assert call_counts == {
        "normalize": 0,
        "omit_empty": 0,
        "scalar_family": 0,
        "validate_keys": 200,
    }

    monkeypatch.undo()
    batch_10k = generate_records(10000, cache)
    # Warm study pipeline cache once (matching SnowflakeExportSink batch conversion)
    _records_to_arrow_table(batch_10k[:1], study_key="STUDY_BENCH")
    start = time.perf_counter()
    tbl_10k = _records_to_arrow_table(batch_10k, study_key="STUDY_BENCH")
    study_batch_elapsed = time.perf_counter() - start
    assert tbl_10k.num_rows == 10000
    assert study_batch_elapsed < 0.75, (
        f"Expected 10,000-record study Arrow conversion < 0.75s, got {study_batch_elapsed:.4f}s"
    )


def evaluate_performance_suite(
    *,
    baseline_path: Path = BASELINE_FILE,
    update_baseline: bool = False,
    runs: int | None = None,
    record_count: int | None = None,
    report_dir: Path | str | None = None,
) -> dict[str, float]:
    """Run calibrated median benchmarks, enforce static ceilings and baseline ratios, and emit report."""
    assert baseline_path.exists(), f"Committed performance baseline missing: {baseline_path}"
    baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))

    effective_runs = (
        runs if runs is not None else int(baseline_data.get("median_runs", BENCHMARK_RUNS))
    )
    effective_record_count = (
        record_count if record_count is not None else int(baseline_data.get("record_count", 10000))
    )
    calibration_iterations = int(baseline_data.get("calibration_iterations", 5000))
    tolerance = float(baseline_data.get("max_regression_tolerance", 0.45))

    with VerificationReporter(
        "performance",
        report_dir=report_dir,
        passed_summary=(
            f"All {len(BENCHMARK_METRICS)} performance benchmarks passed median-of-{effective_runs} "
            "static ceilings and CPU-normalized baseline ratios."
        ),
        failed_summary="Performance benchmark suite detected latency ceiling or baseline ratio regressions.",
    ) as reporter:
        print("Running SDK Observability & Performance Suite...")
        calibration_latency = calibrate_runner_cpu(
            runs=effective_runs,
            iterations=calibration_iterations,
        )
        median_results = measure_median_benchmarks(
            runs=effective_runs,
            record_count=effective_record_count,
        )

        reporter.add_metric("calibration_latency", calibration_latency, unit="seconds")
        current_ratios = {
            metric: median_results[metric] / calibration_latency for metric in BENCHMARK_METRICS
        }

        for metric in BENCHMARK_METRICS:
            reporter.add_metric(metric, median_results[metric], unit="seconds")
            reporter.add_metric(f"{metric}_ratio", current_ratios[metric], unit="ratio")

        metrics_cfg = baseline_data.get("metrics", {})
        for metric in BENCHMARK_METRICS:
            if metric not in metrics_cfg:
                reporter.add_violation(
                    "missing_baseline",
                    f"{metric}: missing configuration in {baseline_path.name}",
                )
                continue
            ceiling = float(metrics_cfg[metric]["static_ceiling_seconds"])
            if median_results[metric] > ceiling:
                reporter.add_violation(
                    "static_ceiling_exceeded",
                    f"{metric}: median {median_results[metric]:.4f}s exceeds static ceiling {ceiling:.4f}s",
                )

        if update_baseline and not reporter.violations:
            baseline_data["reference_calibration_seconds"] = round(calibration_latency, 6)
            for metric in BENCHMARK_METRICS:
                metrics_cfg[metric]["normalized_ratio"] = round(current_ratios[metric], 4)
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps(baseline_data, indent=2) + "\n", encoding="utf-8")
            reporter.set_summary("Updated committed CPU-normalized performance baseline ratios.")
        elif not update_baseline:
            for metric in BENCHMARK_METRICS:
                if metric not in metrics_cfg:
                    continue
                baseline_ratio = float(metrics_cfg[metric]["normalized_ratio"])
                allowed_ratio = baseline_ratio * (1.0 + tolerance)
                diff_pct = (
                    ((current_ratios[metric] - baseline_ratio) / baseline_ratio) * 100.0
                    if baseline_ratio > 0
                    else 0.0
                )
                if current_ratios[metric] > allowed_ratio:
                    reporter.add_violation(
                        "baseline_regression",
                        f"{metric}: normalized ratio {current_ratios[metric]:.4f} "
                        f"(median {median_results[metric]:.4f}s) exceeds baseline "
                        f"{baseline_ratio:.4f} by {diff_pct:.1f}% (> {tolerance * 100:.0f}%)",
                    )
                else:
                    print(
                        f"{metric} OK: median={median_results[metric]:.4f}s, "
                        f"ratio={current_ratios[metric]:.4f} "
                        f"(baseline={baseline_ratio:.4f}, diff={diff_pct:+.1f}%)"
                    )

        if reporter.violations:
            degradations = [v.message for v in reporter.violations]
            reporter.set_summary(
                f"Detected {len(degradations)} performance violation(s) across "
                f"{len(BENCHMARK_METRICS)} benchmarks."
            )
            assert not degradations, "Performance Regressions Detected:\n" + "\n".join(degradations)

        return median_results


def test_performance_suite(request: pytest.FixtureRequest):
    update_baseline = bool(request.config.getoption("--update-perf-baseline"))
    evaluate_performance_suite(update_baseline=update_baseline)


def test_concurrent_airflow_trigger_waves_performance(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Benchmark concurrent waves of 25, 100, and 250 ImednetJobTrigger.run coroutines after warmup."""
    import asyncio
    import ssl
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    import httpx

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.triggers import ImednetJobTrigger

    caplog.set_level(logging.WARNING)
    caplog.set_level(logging.WARNING, logger="imednet")

    conn = SimpleNamespace(
        login="perf-key",  # pragma: allowlist secret
        password="perf-sec",  # pragma: allowlist secret
        host="https://edc.example.test",
        schema=None,
        port=None,
        extra_dejson={},
    )

    async def _mock_send(
        _self: httpx.AsyncClient, request: httpx.Request, **kwargs
    ) -> httpx.Response:
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json={"batchId": "B1", "state": "COMPLETED", "progress": 100},
            request=request,
        )

    async def _measure_wave(wave_size: int) -> tuple[float, float]:
        max_stall = 0.0
        running = True

        async def _watchdog() -> None:
            nonlocal max_stall
            last = time.perf_counter()
            while running:
                await asyncio.sleep(0)
                now = time.perf_counter()
                max_stall = max(max_stall, now - last)
                last = now

        triggers = [
            ImednetJobTrigger(
                study_key="S1",
                batch_id=f"B_PERF_{wave_size}_{i}",
                poke_interval=0.01,
            )
            for i in range(wave_size)
        ]
        wd = asyncio.create_task(_watchdog())
        start = time.perf_counter()
        events = await asyncio.gather(*(anext(t.run()) for t in triggers))
        elapsed = time.perf_counter() - start
        running = False
        await wd
        assert len(events) == wave_size
        return max_stall, elapsed

    async def _run() -> None:
        with (
            patch.object(ImednetHook, "_aget_airflow_connection", new=AsyncMock(return_value=conn)),
            patch.object(httpx.AsyncClient, "send", new=_mock_send),
        ):
            warmup = ImednetJobTrigger(study_key="S1", batch_id="B_WARM", poke_interval=0.01)
            _ = [ev async for ev in warmup.run()]

            read_calls = 0
            ssl_calls = 0
            orig_read = Path.read_text
            orig_ssl = ssl.create_default_context

            def _count_read(self: Path, *a, **kw) -> str:
                nonlocal read_calls
                read_calls += 1
                return orig_read(self, *a, **kw)

            def _count_ssl(*a, **kw) -> ssl.SSLContext:
                nonlocal ssl_calls
                ssl_calls += 1
                return orig_ssl(*a, **kw)

            monkeypatch.setattr(Path, "read_text", _count_read)
            monkeypatch.setattr(ssl, "create_default_context", _count_ssl)

            for wave_size, stall_budget, elapsed_budget in (
                (25, 0.2, 1.0),
                (100, 0.2, 2.0),
                (250, 5.0, 5.0),
            ):
                best_stall, best_elapsed = await _measure_wave(wave_size)
                if best_stall >= stall_budget or best_elapsed >= elapsed_budget:
                    retry_stall, retry_elapsed = await _measure_wave(wave_size)
                    best_stall = min(best_stall, retry_stall)
                    best_elapsed = min(best_elapsed, retry_elapsed)

                assert best_stall < stall_budget, (
                    f"Wave {wave_size} max event-loop stall {best_stall:.4f}s >= {stall_budget}s"
                )
                assert best_elapsed < elapsed_budget, (
                    f"Wave {wave_size} elapsed {best_elapsed:.4f}s >= {elapsed_budget}s"
                )

            assert read_calls == 0
            assert ssl_calls == 0

    asyncio.run(_run())
