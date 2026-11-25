"""Unit and regression tests for consolidated CLI batch, preflight, and job progress helpers."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.unit.cli.test_cli import CliRunner

from imednet import ImednetSDK, cli
from imednet.cli.utils import _write_batch, write_helpers
from imednet.cli.utils.output import format_job_progress
from imednet.cli.utils.write_helpers import make_job_progress_callback, merge_inline_data_overrides
from imednet.core.client import Client
from imednet.core.context import reset_strict_mode_context, set_strict_mode_context
from imednet.datadict.models import DataDictionary, Form, Variable
from imednet.errors import AuthenticationError, PathTraversalValidationError, ServerError
from imednet.models.jobs import Job, JobState, JobStatus
from imednet.simulation import SimulationState, SimulationTransport
from imednet.spi import cli as spi_cli
from imednet.utils.batch_coordinator import BatchSubmissionSummary
from imednet.utils.job_poller import JobStatusEvent


class _CliResult:
    """Captured CLI invocation result."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _invoke_cli(capsys: pytest.CaptureFixture[str], args: list[str]) -> _CliResult:
    capsys.readouterr()
    exit_code = 0
    try:
        cli.app(args)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return _CliResult(exit_code, captured.out, captured.err)


@pytest.fixture(autouse=True)
def _reset_env_and_cli(monkeypatch: pytest.MonkeyPatch):
    """Provide clean CLI parser and credentials per test."""
    token = set_strict_mode_context(False)
    monkeypatch.setenv("IMEDNET_API_KEY", "sim-key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "sim-secret")  # pragma: allowlist secret
    importlib.reload(cli)
    try:
        yield
    finally:
        importlib.reload(cli)
        reset_strict_mode_context(token)


@pytest.fixture
def sim_sdk(monkeypatch: pytest.MonkeyPatch) -> ImednetSDK:
    """Patch get_sdk to return a SimulationTransport-backed ImednetSDK."""
    state = SimulationState()
    base_transport = SimulationTransport(state=state)

    def _build_sdk() -> ImednetSDK:
        return ImednetSDK(
            client=Client(
                api_key="sim-key",  # pragma: allowlist secret
                security_key="sim-secret",  # pragma: allowlist secret
                base_url="https://edc.sim.imednet.com",
                transport=base_transport,
            )
        )

    sdk = _build_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda **_kwargs: _build_sdk())
    return sdk


def test_merge_inline_data_overrides_preserves_original_and_coerces_per_form() -> None:
    """merge_inline_data_overrides copies payloads, merges recordData/data, and coerces per form."""
    dd = DataDictionary(
        forms=[
            Form(
                formId=77,
                formKey="F_COERCE",
                formName="Coerce Form",
                variables=[
                    Variable(variableName="V_ZIP", variableType="text"),
                    Variable(variableName="V_COUNT", variableType="integer"),
                ],
            ),
            Form(
                formId=88,
                formKey="F_OTHER",
                formName="Other Form",
                variables=[
                    Variable(variableName="V_ZIP", variableType="integer"),
                ],
            ),
        ]
    )

    original = {"recordId": 101, "recordData": {"V_EXISTING": "keep", "V_COUNT": 1}}
    noop_copy = merge_inline_data_overrides(original, None, schema=dd)
    assert noop_copy == original
    assert noop_copy is not original

    merged = merge_inline_data_overrides(
        original,
        ["V_ZIP=02138", "V_COUNT=9"],
        schema=dd,
        default_form_id=77,
    )
    assert original["recordData"] == {"V_EXISTING": "keep", "V_COUNT": 1}
    assert "data" not in original
    assert merged["data"] == {"V_EXISTING": "keep", "V_ZIP": "02138", "V_COUNT": 9}

    # Record-level formId is not overridden by a different default_form_key
    explicit_fid = merge_inline_data_overrides(
        {"formId": 77, "data": {}},
        ["V_ZIP=02138"],
        schema=dd,
        default_form_key="F_OTHER",
    )
    assert explicit_fid["data"] == {"V_ZIP": "02138"}

    # Fallback default_form_key is applied when record specifies neither formKey nor formId
    fallback_fk = merge_inline_data_overrides(
        original,
        ["V_ZIP=02138"],
        schema=dd,
        default_form_key="F_COERCE",
    )
    assert fallback_fk["data"] == {"V_EXISTING": "keep", "V_COUNT": 1, "V_ZIP": "02138"}


def test_subjects_register_skip_existing_preserves_skipped_count_on_summary_and_partial_errors(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sim_sdk: ImednetSDK,
) -> None:
    """subjects register --skip-existing sets skipped_count on BatchSubmissionSummary and partial errors."""
    monkeypatch.setattr(sim_sdk, "close", lambda: None)
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda **_kwargs: sim_sdk)

    csv_path = tmp_path / "skip_check.csv"
    csv_path.write_text(
        "formKey,siteName,subjectKey,V_AGE\nF_DM,SITE,SUB1,30\nF_DM,SITE,SUB-BRAND-NEW,44\n",
        encoding="utf-8",
    )

    def _fake_create_batch(*_args: object, **_kwargs: object) -> BatchSubmissionSummary:
        return BatchSubmissionSummary(
            study_key="STUDY",
            jobs=[Job(jobId="J-SKIP-1", batchId="B-SKIP-1", state="QUEUED")],
            chunk_sizes=[1],
            total_records=1,
            skipped_count=0,
        )

    monkeypatch.setattr(sim_sdk.records, "create_batch", _fake_create_batch)

    res = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--file",
            str(csv_path),
            "--skip-existing",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res.exit_code == 0, res.stderr
    item = json.loads(res.stdout)["items"][0]
    assert item["totalRecords"] == 1
    assert item["skippedCount"] == 1
    assert item["batchIds"] == ["B-SKIP-1"]

    # Partial mid-batch error also retains skipped_count when propagated through execute_cli_write
    def _failing_create_batch(*_args: object, **_kwargs: object) -> BatchSubmissionSummary:
        err = ServerError("Chunk 2 failed", status_code=503)
        err.partial_summary = BatchSubmissionSummary(  # type: ignore[attr-defined]
            study_key="STUDY",
            jobs=[Job(jobId="J-PART-1", batchId="B-PART-1", state="QUEUED")],
            chunk_sizes=[1],
            total_records=1,
            skipped_count=0,
            unsubmitted_chunks=[[{"formKey": "F_DM", "data": {"V_AGE": 44}}]],
            has_error=True,
        )
        raise err

    monkeypatch.setattr(sim_sdk.records, "create_batch", _failing_create_batch)
    res_fail = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--file",
            str(csv_path),
            "--skip-existing",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_fail.exit_code == 1
    fail_item = json.loads(res_fail.stdout)["items"][0]
    assert fail_item["skippedCount"] == 1
    assert len(fail_item["unsubmittedChunks"]) == 1


def test_prepare_write_cli_preflight_reexports_and_import_progress_context_ordering(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sim_sdk: ImednetSDK,
) -> None:
    """Preflight helper validates paths, write_helpers lazily exposes batch helpers, and import runs in progress."""
    _ = sim_sdk
    import imednet.cli.records as records_cli_mod

    # 1. spi.cli exports remain unchanged and do not leak internal batch helpers
    for internal_name in (
        "make_job_progress_callback",
        "prepare_write_cli_preflight",
        "merge_inline_data_overrides",
        "run_cli_batch_write",
    ):
        assert internal_name not in spi_cli.__all__
        assert not hasattr(spi_cli, internal_name)

    # 2. write_helpers lazily resolves prepare_write_cli_preflight and run_cli_batch_write from _write_batch
    assert "prepare_write_cli_preflight" in dir(write_helpers)
    assert "run_cli_batch_write" in dir(write_helpers)
    assert write_helpers.prepare_write_cli_preflight is _write_batch.prepare_write_cli_preflight
    assert write_helpers.run_cli_batch_write is _write_batch.run_cli_batch_write
    with pytest.raises(AttributeError):
        _ = write_helpers.non_existent_batch_symbol

    # 3. prepare_write_cli_preflight rejects "-" when allow_stdin_dash=False
    parser = cli.get_parser()
    ns_dash = parser.parse_args(["records", "import", "STUDY", "-"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc_info:
        _write_batch.prepare_write_cli_preflight(
            parser,
            ns_dash,
            file_arg=ns_dash.file,
            allow_stdin_dash=False,
        )
    assert exc_info.value.code == 1
    assert "Input file not found: '-'" in capsys.readouterr().err

    # 4. import_records_cmd loads CSV and JSON files while inside write_progress_context
    in_progress = False
    observed_in_progress: list[bool] = []
    orig_progress_ctx = write_helpers.write_progress_context

    @contextmanager
    def _spy_progress_ctx(*p_args: object, **p_kwargs: object):
        nonlocal in_progress
        with orig_progress_ctx(*p_args, **p_kwargs) as cb:  # type: ignore[arg-type]
            in_progress = True
            try:
                yield cb
            finally:
                in_progress = False

    orig_csv = records_cli_mod.import_records_csv
    orig_json_loader = records_cli_mod.load_records_from_path_or_stdin

    def _spy_csv(*c_args: object, **c_kwargs: object):
        observed_in_progress.append(in_progress)
        return orig_csv(*c_args, **c_kwargs)  # type: ignore[arg-type]

    def _spy_json_loader(*l_args: object, **l_kwargs: object):
        observed_in_progress.append(in_progress)
        return orig_json_loader(*l_args, **l_kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(write_helpers, "write_progress_context", _spy_progress_ctx)
    monkeypatch.setattr(records_cli_mod, "import_records_csv", _spy_csv)
    monkeypatch.setattr(records_cli_mod, "load_records_from_path_or_stdin", _spy_json_loader)

    csv_file = tmp_path / "order.csv"
    csv_file.write_text("formKey,subjectKey,siteName,V_AGE\nF_DM,SUB1,SITE,40\n", encoding="utf-8")
    json_file = tmp_path / "order.json"
    json_file.write_text(
        json.dumps(
            [{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "data": {"V_AGE": 41}}]
        ),
        encoding="utf-8",
    )

    for target in (csv_file, json_file):
        res = _invoke_cli(
            capsys,
            ["records", "import", "STUDY", str(target), "--no-interactive", "--format", "json"],
        )
        assert res.exit_code == 0, res.stderr

    assert observed_in_progress == [True, True]


def test_run_cli_batch_write_preserves_upstream_path_traversal_auth_and_offline_json_schema_contracts(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_cli_batch_write preserves #2127 PathTraversalValidationError/empty-stdout and #2128 JSON studyKey."""
    parser = cli.get_parser()
    ns = parser.parse_args(
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "F_DM",
            "-d",
            "V_AGE=30",
            "--no-interactive",
            "--format",
            "json",
        ]
    )
    ns.study_key = "STUDY"

    # 1. Missing builder argument raises ValueError routed via execute_cli_write validation handler
    capsys.readouterr()
    with pytest.raises(SystemExit) as missing_builder_exit:
        _write_batch.run_cli_batch_write(
            MagicMock(spec=ImednetSDK),
            ns,
            resource="records_create",
            action_label="record submission",
        )
    assert missing_builder_exit.value.code == 1
    assert "One of prepare_payload, prepare_batch, or submit_in_progress is required" in (
        capsys.readouterr().err
    )

    # 2. PathTraversalValidationError inside prepare_payload re-raises without writing to JSON stdout (#2127)
    capsys.readouterr()
    with pytest.raises(PathTraversalValidationError):
        _write_batch.run_cli_batch_write(
            MagicMock(spec=ImednetSDK),
            ns,
            resource="records_create",
            action_label="record submission",
            prepare_payload=lambda _schema: (_ for _ in ()).throw(
                PathTraversalValidationError("Unsafe path segment")
            ),
        )
    assert capsys.readouterr().out == ""

    # 3. Pre-write AuthenticationError leaves stdout empty under --format json (#2127)
    capsys.readouterr()
    with pytest.raises(SystemExit) as auth_exit:
        _write_batch.run_cli_batch_write(
            MagicMock(spec=ImednetSDK),
            ns,
            resource="records_create",
            action_label="record submission",
            prepare_payload=lambda _schema: (_ for _ in ()).throw(
                AuthenticationError("Invalid credentials", status_code=401)
            ),
        )
    assert auth_exit.value.code == 1
    auth_captured = capsys.readouterr()
    assert auth_captured.out == ""
    assert "Authentication Failed:" in auth_captured.err

    # 4. Offline --dry-run on records update runs without credentials and propagates study_key to JSON schema (#2128)
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    dd_file = tmp_path / "upd_dd.json"
    dd_file.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [{"variableName": "V_AGE", "variableType": "integer"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded_dd = write_helpers.load_offline_schema(str(dd_file), study_key="STUDY-OFFLINE")
    assert getattr(loaded_dd, "metadata").studyKey == "STUDY-OFFLINE"

    res_upd_dry = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY-OFFLINE",
            "--record-id",
            "601",
            "--form-key",
            "F_DM",
            "-d",
            "V_AGE=50",
            "--schema",
            str(dd_file),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_upd_dry.exit_code == 0, res_upd_dry.stderr
    parsed_dry = json.loads(res_upd_dry.stdout)["items"][0]
    assert parsed_dry["dryRun"] is True
    assert parsed_dry["studyKey"] == "STUDY-OFFLINE"


def test_make_job_progress_callback_updates_status_ctrl_and_live_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """make_job_progress_callback updates status_ctrl or live stderr and backs job_wait."""
    event = JobStatusEvent(
        batch_id="B-CB-1",
        status=JobStatus(batch_id="B-CB-1", state=JobState.PROCESSING, progress=50),
        poll_number=1,
        elapsed=1.2,
        is_terminal=False,
    )
    expected_msg = format_job_progress(event)

    # 1. Updates status_ctrl when .update is available
    ctrl = MagicMock()
    cb_with_ctrl = make_job_progress_callback(ctrl)
    cb_with_ctrl(event)
    ctrl.update.assert_called_once_with(expected_msg)

    # 2. Writes carriage-return ANSI clear line to live stderr when status_ctrl is None
    buf = io.StringIO()
    monkeypatch.setattr(write_helpers, "supports_live_status", lambda _stream: True)
    monkeypatch.setattr(write_helpers.sys, "stderr", buf)
    cb_live = make_job_progress_callback(None)
    cb_live(event)
    assert buf.getvalue() == f"\r\033[K{expected_msg}"

    # 3. Swallows BrokenPipeError cleanly on closed stderr pipe
    class _BrokenStderr:
        def write(self, _s: str) -> int:
            raise BrokenPipeError("pipe closed")

        def flush(self) -> None:
            pass

    monkeypatch.setattr(write_helpers.sys, "stderr", _BrokenStderr())
    cb_live(event)

    # 4. job_wait passes make_job_progress_callback(status_ctrl) inside fetching_status under --format json
    runner = CliRunner()
    completed = JobStatus(batch_id="B-CB-JSON", state="COMPLETED", progress=100)
    seen_ctrls: list[object] = []

    orig_factory = write_helpers.make_job_progress_callback

    def _spy_factory(status_ctrl: object = None):
        seen_ctrls.append(status_ctrl)
        return orig_factory(status_ctrl)

    mock_sdk = MagicMock()
    mock_sdk.poll_job.return_value = completed
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk),
        patch("imednet.cli.jobs.make_job_progress_callback", side_effect=_spy_factory),
    ):
        res = runner.invoke(
            cli.app, ["jobs", "wait", "STUDY-TEST", "B-CB-JSON", "--format", "json"]
        )

    assert res.exit_code == 0
    assert len(seen_ctrls) == 1
    assert hasattr(seen_ctrls[0], "update")
    parsed = json.loads(res.stdout)
    assert parsed["items"][0]["batchId"] == "B-CB-JSON"


def test_cli_package_has_zero_pylint_r0801_duplicate_code() -> None:
    """CLI package has zero duplicate-code blocks at min-similarity-lines=12 and 8."""
    cli_dir = Path(cli.__file__).resolve().parent
    py_files = sorted(cli_dir.rglob("*.py"))
    assert py_files

    # Always verify via AST-aware normalized line windows (works in wheel/CI test envs without pylint)
    normalized_by_file: dict[Path, list[str]] = {}
    for path in py_files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        skip_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.end_lineno:
                skip_lines.update(range(node.lineno, node.end_lineno + 1))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.body:
                    sig_end = node.body[0].lineno - 1
                    skip_lines.update(range(node.lineno, sig_end + 1))
                    first_stmt = node.body[0]
                    if (
                        isinstance(first_stmt, ast.Expr)
                        and isinstance(first_stmt.value, ast.Constant)
                        and isinstance(first_stmt.value.value, str)
                        and first_stmt.end_lineno
                    ):
                        skip_lines.update(range(first_stmt.lineno, first_stmt.end_lineno + 1))
            elif isinstance(node, (ast.Module, ast.ClassDef)) and node.body:
                first_stmt = node.body[0]
                if (
                    isinstance(first_stmt, ast.Expr)
                    and isinstance(first_stmt.value, ast.Constant)
                    and isinstance(first_stmt.value.value, str)
                    and first_stmt.end_lineno
                ):
                    skip_lines.update(range(first_stmt.lineno, first_stmt.end_lineno + 1))

        kept: list[str] = []
        for idx, raw_line in enumerate(source.splitlines(), start=1):
            if idx in skip_lines:
                continue
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            kept.append(stripped)
        normalized_by_file[path] = kept

    window = 8
    seen_windows: dict[tuple[str, ...], Path] = {}
    for path, lines in normalized_by_file.items():
        for i in range(len(lines) - window + 1):
            chunk = tuple(lines[i : i + window])
            prev_path = seen_windows.get(chunk)
            if prev_path is not None and prev_path != path:
                pytest.fail(
                    f"Duplicate 8-line CLI block between {prev_path.relative_to(cli_dir)} "
                    f"and {path.relative_to(cli_dir)}: {chunk}"
                )
            seen_windows.setdefault(chunk, path)

    pylint_cmd: list[str] | None = None
    if importlib.util.find_spec("pylint") is not None:
        pylint_cmd = [sys.executable, "-m", "pylint"]
    elif shutil.which("pylint"):
        pylint_cmd = [shutil.which("pylint") or "pylint"]

    if pylint_cmd is not None:
        for threshold in ("12", "8"):
            proc = subprocess.run(
                [
                    *pylint_cmd,
                    "--disable=all",
                    "--enable=duplicate-code",
                    "--ignore-comments=yes",
                    "--ignore-docstrings=yes",
                    "--ignore-imports=yes",
                    "--ignore-signatures=yes",
                    f"--min-similarity-lines={threshold}",
                    str(cli_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr
            assert "R0801" not in proc.stdout
