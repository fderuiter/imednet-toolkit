"""Unit tests for unified CLI PHI masking, views, side panels, and record pagination via SPI."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from imednet import ImednetSDK, cli
from imednet.cli.utils.grid import InteractiveDataGrid
from imednet.integrations.enrichment import StudyConfiguration, resolve_study_configuration

MASKED_SENTINEL = "***MASKED***"
import curses

from imednet.models.base import ImednetBaseModel
from imednet.models.queries import Query, QueryComment
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.visits import Visit
from imednet.spi import cli as spi_cli
from imednet.spi.utils import set_base_url_context, set_security_key_context
from imednet_workflows.config_version_control import ConfigVersionStore
from imednet_workflows.data_extraction import AsyncDataExtractionWorkflow, DataExtractionWorkflow
from imednet_workflows.subject_data import SubjectComprehensiveData


class MockWindow:
    """Mock curses window for terminal UI testing."""

    def __init__(self, height: int = 24, width: int = 80) -> None:
        self.height = height
        self.width = width
        self.strings: list[tuple[int, int, str, int]] = []
        self.cursor_pos: tuple[int, int] = (0, 0)
        self.keypad_enabled: bool = False

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.strings.clear()

    def refresh(self) -> None:
        pass

    def keypad(self, flag: bool) -> None:
        self.keypad_enabled = flag

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("Out of bounds")
        if x + len(text) > self.width:
            raise curses.error("String exceeds line width")
        if y == self.height - 1 and x + len(text) == self.width:
            raise curses.error("Bottom-right corner write")
        self.strings.append((y, x, text, attr))

    def move(self, y: int, x: int) -> None:
        self.cursor_pos = (y, x)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in (
        "IMEDNET_API_KEY",
        "IMEDNET_SECURITY_KEY",
        "IMEDNET_OIDC_TOKEN",
        "IMEDNET_BASE_URL",
        "IMEDNET_ENCRYPTION_KEY",
        "IMEDNET_CONFIG_ENCRYPTION_KEY",
        "IMEDNET_CONFIG_DB_PATH",
        "IMEDNET_SIMULATION_MODE",
        "IMEDNET_CLI_OUTPUT_FORMAT",
        "IMEDNET_NON_INTERACTIVE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    yield


def _invoke_cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    exit_code = 0
    try:
        cli.app(args)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _setup_encrypted_store_sdk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    study_key: str = "STUDY_ENC",
    phi_fields: list[str] | None = None,
) -> ImednetSDK:
    """Create an SDK with non-ambient credentials and commit an encrypted StudyConfiguration."""
    db_path = tmp_path / "store" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_path))

    sdk = ImednetSDK(
        api_key="test-api-key-enc",
        security_key="sdk-scoped-secret-key-for-fernet-derivation",
        base_url="https://scoped-origin.imednet.example",
    )
    sdk.get_sites = MagicMock(return_value=[])
    cfg = StudyConfiguration(
        studyKey=study_key,
        phiFields=phi_fields or ["CUSTOM_SECRET_NOTE", "patientAlias"],
    )
    ConfigVersionStore().commit_config(study_key, cfg, "commit phi fields", sdk=sdk)
    return sdk


def test_spi_cli_lazy_exports_and_help_startup_hygiene() -> None:
    """New imednet.spi.cli exports resolve lazily without eager enrichment imports on --help."""
    new_exports = {
        "resolve_cli_phi_fields",
        "mask_cli_models",
        "mask_cli_records",
        "prepare_list_query",
        "collect_list_items_with_remainder",
    }
    assert new_exports.issubset(set(spi_cli.__all__))
    for attr in new_exports:
        assert callable(getattr(spi_cli, attr))

    assert "get_model_value" not in spi_cli.__all__
    assert "register_list_command" not in spi_cli.__all__

    probe = (
        "import sys\n"
        "from imednet import cli\n"
        "for argv in (['--help'], ['workflows', 'extract-records', '--help']):\n"
        "    try:\n"
        "        cli.app(argv)\n"
        "    except SystemExit as exc:\n"
        "        assert exc.code == 0\n"
        "assert 'imednet.integrations.enrichment' not in sys.modules, list(sys.modules)\n"
        "assert 'imednet.cli.utils.export' not in sys.modules, list(sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


def test_encrypted_store_upper_snake_and_camel_case_phi_masked_across_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Encrypted ConfigVersionStore UPPER_SNAKE & camelCase phiFields decrypt via sdk and mask."""
    study_key = "STUDY_ENC"
    sdk = _setup_encrypted_store_sdk(
        monkeypatch,
        tmp_path,
        study_key=study_key,
        phi_fields=["CUSTOM_SECRET_NOTE", "patientAlias"],
    )

    b_tok = set_base_url_context(None)
    s_tok = set_security_key_context(None)
    try:
        # Outside sdk_governance_context, encrypted commit cannot be decrypted
        assert resolve_study_configuration(study_key) is None
        resolved_phi = spi_cli.resolve_cli_phi_fields(study_key, sdk=sdk)
        assert {"customsecretnote", "patientalias"}.issubset(resolved_phi)

        sample_record = Record(
            study_key=study_key,
            record_id=501,
            subject_key="SUBJ-01",
            form_key="DM",
            record_status="Record Complete",
            record_data={
                "usubjid": "STUDY_ENC-SUBJ-01",
                "subjid": "SUBJ-01",
                "studyid": study_key,
                "CUSTOM_SECRET_NOTE": "RawUpperSecret",
                "customSecretNote": "RawCamelVariant",
                "patientAlias": "RawPatientAlias",
                "PATIENT_ALIAS": "RawSnakeVariant",
                "SAFE_METRIC": 98,
            },
        )
        sample_subject = Subject(
            study_key=study_key,
            subject_key="SUBJ-01",
            subject_status="Enrolled",
            site_name="General Hospital",
        )
        sample_query = Query(
            study_key=study_key,
            subject_key="SUBJ-01",
            record_id=501,
            annotation_id=77,
            description="Verify patientAlias=RawPatientAlias and email=jane@example.com",
            query_comments=[
                QueryComment(
                    sequence=1,
                    user="auditor",
                    comment="Confirmed CUSTOM_SECRET_NOTE=RawUpperSecret for SSN 123-45-6789",
                )
            ],
        )

        sdk.records = MagicMock()
        sdk.records.list.return_value = [sample_record]
        sdk.get_records = MagicMock(return_value=[sample_record])
        sdk.get_subjects = MagicMock(return_value=[sample_subject])
        sdk.get_visits = MagicMock(return_value=[])
        sdk.get_queries = MagicMock(return_value=[sample_query])

        monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

        # 1. subject-data JSON masks both UPPER_SNAKE and camelCase + Query text and emits camelCase
        code, out, err = _invoke_cli(
            ["subject-data", study_key, "SUBJ-01", "--format", "json"],
            capsys,
        )
        assert code == 0, err
        payload = json.loads(out)
        item = payload["items"][0]
        assert "subjectDetails" in item
        assert "subject_details" not in item
        assert item["subjectDetails"]["studyKey"] == study_key
        assert item["subjectDetails"]["subjectKey"] == "SUBJ-01"

        rec_out = item["records"][0]
        assert rec_out["recordId"] == 501
        assert rec_out["subjectKey"] == "SUBJ-01"
        rec_data = rec_out["recordData"]
        assert rec_data["usubjid"] == "STUDY_ENC-SUBJ-01"
        assert rec_data["subjid"] == "SUBJ-01"
        assert rec_data["studyid"] == study_key
        assert rec_data["CUSTOM_SECRET_NOTE"] == MASKED_SENTINEL
        assert rec_data["customSecretNote"] == MASKED_SENTINEL
        assert rec_data["patientAlias"] == MASKED_SENTINEL
        assert rec_data["PATIENT_ALIAS"] == MASKED_SENTINEL
        assert rec_data["SAFE_METRIC"] == 98

        q_out = item["queries"][0]
        assert "RawPatientAlias" not in q_out["description"]
        assert "jane@example.com" not in q_out["description"]
        assert "RawUpperSecret" not in q_out["queryComments"][0]["comment"]
        assert "123-45-6789" not in q_out["queryComments"][0]["comment"]

        # --no-mask-phi preserves raw values
        code_raw, out_raw, _ = _invoke_cli(
            ["subject-data", study_key, "SUBJ-01", "--no-mask-phi", "--format", "json"],
            capsys,
        )
        assert code_raw == 0
        raw_item = json.loads(out_raw)["items"][0]
        assert raw_item["records"][0]["recordData"]["CUSTOM_SECRET_NOTE"] == "RawUpperSecret"
        assert raw_item["records"][0]["recordData"]["patientAlias"] == "RawPatientAlias"
        assert "RawPatientAlias" in raw_item["queries"][0]["description"]
        assert "RawUpperSecret" in raw_item["queries"][0]["queryComments"][0]["comment"]

        # 2. workflows extract-records masks custom PHI & preserves linkage identifiers
        code_ext, out_ext, err_ext = _invoke_cli(
            ["workflows", "extract-records", study_key, "--format", "json"],
            capsys,
        )
        assert code_ext == 0, err_ext
        ext_rec = json.loads(out_ext)["items"][0]
        assert ext_rec["recordId"] == 501
        assert ext_rec["subjectKey"] == "SUBJ-01"
        assert ext_rec["recordData"]["usubjid"] == "STUDY_ENC-SUBJ-01"
        assert ext_rec["recordData"]["CUSTOM_SECRET_NOTE"] == MASKED_SENTINEL
        assert ext_rec["recordData"]["patientAlias"] == MASKED_SENTINEL

        # 3. workflows extract-audit-trail decrypts custom PHI via sdk for JSON and ODM exports
        rev = RecordRevision(
            study_key=study_key,
            record_revision_id=11,
            record_id=501,
            subject_key="SUBJ-01",
            form_key="DM",
            user="coord1",
            reason_for_change="Updated patientAlias=RawPatientAlias per chart",
        )
        sdk.record_revisions = MagicMock()
        sdk.record_revisions.list.return_value = [rev]
        sdk.get_record_revisions = MagicMock(return_value=[rev])
        sdk.studies = MagicMock()
        sdk.studies.list.return_value = [Study(study_key=study_key, study_name="Encrypted Study")]
        sdk.forms = MagicMock()
        sdk.forms.list.return_value = []
        sdk.variables = MagicMock()
        sdk.variables.list.return_value = []
        sdk.intervals = MagicMock()
        sdk.intervals.list.return_value = []

        code_aud, out_aud, err_aud = _invoke_cli(
            ["workflows", "extract-audit-trail", study_key, "--format", "json"],
            capsys,
        )
        assert code_aud == 0, err_aud
        assert "RawPatientAlias" not in out_aud

        odm_path = tmp_path / "audit.odm.xml"
        code_odm, _, err_odm = _invoke_cli(
            ["workflows", "extract-audit-trail", study_key, "-o", str(odm_path)],
            capsys,
        )
        assert code_odm == 0, err_odm
        odm_xml = odm_path.read_text(encoding="utf-8")
        assert "RawPatientAlias" not in odm_xml
        assert "RawUpperSecret" not in odm_xml
    finally:
        set_security_key_context(s_tok)
        set_base_url_context(b_tok)


ALL_CORE_LIST_COMMANDS: list[tuple[str, bool, bool]] = [
    ("studies", False, False),
    ("sites", True, False),
    ("forms", True, False),
    ("intervals", True, False),
    ("variables", True, False),
    ("visits", True, False),
    ("subjects", True, False),
    ("records", True, True),
    ("record-revisions", True, True),
    ("queries", True, False),
    ("codings", True, False),
    ("users", True, False),
]


@pytest.mark.parametrize(("resource_cmd", "needs_study", "supports_output"), ALL_CORE_LIST_COMMANDS)
def test_all_twelve_core_list_commands_accept_mask_phi_and_scope_output_flag(
    resource_cmd: str,
    needs_study: bool,
    supports_output: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All 12 core list commands accept --mask-phi/--no-mask-phi; -o stays scoped."""
    monkeypatch.chdir(tmp_path)
    sdk = MagicMock()
    attr_name = resource_cmd.replace("-", "_")
    getattr(sdk, attr_name).list.return_value = []
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    base_args = [resource_cmd, "list"] + (["STUDY-1"] if needs_study else [])

    for flag in ("--mask-phi", "--no-mask-phi"):
        code, _, err = _invoke_cli([*base_args, flag, "--format", "json"], capsys)
        assert code == 0, f"{resource_cmd} list rejected {flag}: {err}"

    code_out, _, _ = _invoke_cli([*base_args, "-o", "json"], capsys)
    if supports_output:
        assert code_out == 0
    else:
        assert code_out == 2


def test_users_and_queries_list_mask_phi_across_formats_and_grid_load_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """users list masks User.email and queries list scrubs Query comments across pages and [a]."""
    study_key = "STUDY_ENC"
    sdk = _setup_encrypted_store_sdk(
        monkeypatch,
        tmp_path,
        study_key=study_key,
        phi_fields=["patientAlias"],
    )

    users = [
        User(
            user_id="U-1",
            login="jdoe",
            first_name="Jane",
            last_name="Doe",
            email="jane.doe@clinic.org",
            user_active_in_study=True,
        ),
        User(
            user_id="U-2",
            login="bsmith",
            first_name="Bob",
            last_name="Smith",
            email="bob.smith@clinic.org",
            user_active_in_study=True,
        ),
    ]
    queries = [
        Query(
            study_key=study_key,
            subject_key="SUBJ-1",
            record_id=10,
            annotation_id=1,
            type="question",
            description="Check patientAlias=AlphaSecret and email=alpha@clinic.org",
            query_comments=[
                QueryComment(
                    sequence=1,
                    user="cra1",
                    comment="Follow-up on patientAlias=AlphaSecret",
                )
            ],
        ),
        Query(
            study_key=study_key,
            subject_key="SUBJ-2",
            record_id=20,
            annotation_id=2,
            type="question",
            description="Check patientAlias=BetaSecret and email=beta@clinic.org",
            query_comments=[
                QueryComment(
                    sequence=1,
                    user="cra2",
                    comment="Follow-up on patientAlias=BetaSecret",
                )
            ],
        ),
    ]

    sdk.users = MagicMock()
    sdk.users.list.side_effect = lambda *a, **kw: iter(users)
    sdk.queries = MagicMock()
    sdk.queries.list.side_effect = lambda *a, **kw: iter(queries)
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    b_tok = set_base_url_context(None)
    s_tok = set_security_key_context(None)
    try:
        # 1. users list table mode masks email
        code_tbl, out_tbl, _ = _invoke_cli(
            ["users", "list", study_key, "--no-interactive"],
            capsys,
        )
        assert code_tbl == 0
        assert "jane.doe@clinic.org" not in out_tbl
        assert MASKED_SENTINEL in out_tbl

        # 2. users list JSON mode masks email
        code_json, out_json, _ = _invoke_cli(
            ["users", "list", study_key, "--format", "json"],
            capsys,
        )
        assert code_json == 0
        user_items = json.loads(out_json)["items"]
        assert [u["email"] for u in user_items] == [MASKED_SENTINEL, MASKED_SENTINEL]

        # 3. users list interactive grid initial page + [a] remainder loader masks email
        captured_grid: dict[str, Any] = {}

        def _capture_grid(**kwargs: Any) -> bool:
            captured_grid.update(kwargs)
            return True

        with (
            patch("imednet.cli.utils.grid.is_interactive_terminal", return_value=True),
            patch("imednet.cli.utils.grid.run_interactive_grid", side_effect=_capture_grid),
        ):
            code_grid, _, _ = _invoke_cli(
                ["users", "list", study_key, "--limit", "1"],
                capsys,
            )
        assert code_grid == 0
        assert captured_grid["truncated"] is True
        assert captured_grid["items"][0]["email"] == MASKED_SENTINEL
        assert captured_grid["details"][0].email == MASKED_SENTINEL
        more_rows, more_details = captured_grid["on_load_all"]()
        assert more_rows[0]["email"] == MASKED_SENTINEL
        assert more_details[0].email == MASKED_SENTINEL

        # 4. users list --no-mask-phi preserves email
        code_unmasked, out_unmasked, _ = _invoke_cli(
            ["users", "list", study_key, "--no-mask-phi", "--format", "json"],
            capsys,
        )
        assert code_unmasked == 0
        assert json.loads(out_unmasked)["items"][0]["email"] == "jane.doe@clinic.org"

        # 5. queries list JSON + interactive [a] scrubs Query.description and QueryComment.comment
        code_q_json, out_q_json, _ = _invoke_cli(
            ["queries", "list", study_key, "--format", "json"],
            capsys,
        )
        assert code_q_json == 0
        q_items = json.loads(out_q_json)["items"]
        for q_item in q_items:
            assert "AlphaSecret" not in q_item["description"]
            assert "BetaSecret" not in q_item["description"]
            assert "clinic.org" not in q_item["description"]
            assert "AlphaSecret" not in q_item["queryComments"][0]["comment"]
            assert "BetaSecret" not in q_item["queryComments"][0]["comment"]

        captured_q_grid: dict[str, Any] = {}
        with (
            patch("imednet.cli.utils.grid.is_interactive_terminal", return_value=True),
            patch(
                "imednet.cli.utils.grid.run_interactive_grid",
                side_effect=lambda **kw: captured_q_grid.update(kw) or True,
            ),
        ):
            code_q_grid, _, _ = _invoke_cli(
                ["queries", "list", study_key, "--limit", "1"],
                capsys,
            )
        assert code_q_grid == 0
        assert "AlphaSecret" not in captured_q_grid["items"][0]["description"]
        assert "AlphaSecret" not in captured_q_grid["details"][0].query_comments[0].comment
        more_q_rows, more_q_details = captured_q_grid["on_load_all"]()
        assert "BetaSecret" not in more_q_rows[0]["description"]
        assert "BetaSecret" not in more_q_details[0].query_comments[0].comment

        # 6. queries list --no-mask-phi skips scrubbing
        code_q_raw, out_q_raw, _ = _invoke_cli(
            ["queries", "list", study_key, "--no-mask-phi", "--format", "json"],
            capsys,
        )
        assert code_q_raw == 0
        raw_q = json.loads(out_q_raw)["items"][0]
        assert "AlphaSecret" in raw_q["description"]
        assert "AlphaSecret" in raw_q["queryComments"][0]["comment"]
    finally:
        set_security_key_context(s_tok)
        set_base_url_context(b_tok)


def test_subject_comprehensive_data_model_aliases_and_python_attribute_access() -> None:
    """SubjectComprehensiveData subclasses ImednetBaseModel with camelCase serialization."""
    assert issubclass(SubjectComprehensiveData, ImednetBaseModel)
    subj = Subject(study_key="S1", subject_key="SUB-1", subject_status="Enrolled")

    by_field = SubjectComprehensiveData(subject_details=subj)
    assert by_field.subject_details is subj
    dumped = by_field.model_dump(by_alias=True)
    assert "subjectDetails" in dumped
    assert "subject_details" not in dumped

    by_alias = SubjectComprehensiveData(subjectDetails=subj)
    assert by_alias.subject_details is subj


def test_interactive_grid_side_panel_nested_subject_data_and_extract_records_inspection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Side panel formats nested list[dict] cleanly without repr braces and shows RECORD INSPECTION."""
    comp = SubjectComprehensiveData(
        subject_details=Subject(
            study_key="STUDY-1",
            subject_key="SUBJ-1",
            subject_status="Enrolled",
            site_name="Site A",
        ),
        visits=[Visit(study_key="STUDY-1", subject_key="SUBJ-1", interval_name="Screening")],
        records=[
            Record(
                study_key="STUDY-1",
                record_id=301,
                subject_key="SUBJ-1",
                form_key="VS",
                record_status="Record Complete",
                record_data={
                    "SYSBP": 120,
                    "VITALS_NESTED": {"HR": 72, "TEMP": 36.7},
                    "FLAGS": ["baseline", "verified"],
                },
            )
        ],
        queries=[
            Query(
                study_key="STUDY-1",
                subject_key="SUBJ-1",
                record_id=301,
                annotation_id=88,
                description="Verify blood pressure",
                query_comments=[
                    QueryComment(sequence=1, user="cra1", comment="Confirmed with source")
                ],
            )
        ],
    )

    win = MockWindow(height=55, width=110)
    subj_grid = InteractiveDataGrid(
        win,
        items=[{"Subject": "SUBJ-1", "Status": "Enrolled", "Records": 1, "Queries": 1}],
        fields=["Subject", "Status", "Records", "Queries"],
        label="subject overview",
        details=[comp],
    )
    subj_grid._handle_normal_input(" ")
    assert subj_grid.side_panel_open is True
    subj_grid.draw()

    panel_x = int(110 * 0.55)
    panel_lines = [text.rstrip() for y, x, text, _ in win.strings if y >= 2 and x == panel_x + 2]
    panel_text = "\n".join(panel_lines)
    assert "SYSBP: 120" in panel_text
    assert "HR: 72" in panel_text
    assert "Confirmed with source" in panel_text
    assert "{" not in panel_text
    assert "}" not in panel_text

    # workflows extract-records uses "matching records" table label while opening RECORD INSPECTION
    rec = Record(
        study_key="STUDY-1",
        record_id=401,
        subject_key="SUBJ-9",
        form_key="AE",
        record_status="Record Complete",
        record_data={"AETERM": "Headache", "AESER": "No"},
    )
    sdk = MagicMock()
    sdk.records.list.return_value = [rec]
    sdk.get_records.return_value = [rec]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    # Non-interactive table keeps "matching records"
    code_tbl, out_tbl, _ = _invoke_cli(
        ["workflows", "extract-records", "STUDY-1", "--no-interactive"],
        capsys,
    )
    assert code_tbl == 0
    assert "Found 1 matching records:" in out_tbl

    captured_ext_grid: dict[str, Any] = {}
    with (
        patch("imednet.cli.utils.grid.is_interactive_terminal", return_value=True),
        patch(
            "imednet.cli.utils.grid.run_interactive_grid",
            side_effect=lambda **kw: captured_ext_grid.update(kw) or True,
        ),
    ):
        code_ig, _, _ = _invoke_cli(["workflows", "extract-records", "STUDY-1"], capsys)
    assert code_ig == 0
    assert captured_ext_grid["label"] == "matching records"
    assert captured_ext_grid["entity_kind"] == "records"

    rec_win = MockWindow(height=35, width=100)
    rec_grid = InteractiveDataGrid(
        rec_win,
        items=captured_ext_grid["items"],
        fields=captured_ext_grid["fields"],
        label=captured_ext_grid["label"],
        details=captured_ext_grid["details"],
        entity_kind=captured_ext_grid["entity_kind"],
    )
    rec_grid._handle_normal_input(" ")
    rec_grid.draw()
    rec_panel_x = int(100 * 0.55)
    rec_panel_text = "\n".join(
        text.rstrip() for y, x, text, _ in rec_win.strings if y >= 0 and x == rec_panel_x + 2
    )
    grid_header_text = "\n".join(text for y, x, text, _ in rec_win.strings if y == 0)
    assert "matching records" in grid_header_text.lower()
    assert "RECORD INSPECTION" in rec_panel_text
    assert "RECORD METADATA" in rec_panel_text
    assert "FORM VARIABLES" in rec_panel_text
    assert "Headache" in rec_panel_text


@pytest.mark.asyncio
async def test_data_extraction_record_data_filter_dedup_fallback_and_cli_lazy_pagination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """extract_records_by_criteria deduplicates record_data_filter and CLI streams bounded pages."""
    captured_calls: list[dict[str, Any]] = []
    all_records = [
        Record(
            study_key="STUDY_ENC",
            record_id=idx,
            subject_key=f"SUBJ-{idx}",
            form_key="DM",
            record_status="Record Complete",
            record_data={"AGE": 20 + idx, "patientAlias": f"Secret-{idx}"},
        )
        for idx in (1, 2, 3)
    ]

    # 1. Custom minimal ImednetFacade double WITHOUT sdk.records.list falls back to get_records
    class _MinimalSyncFacade:
        def get_records(
            self,
            study_key: str,
            record_data_filter: str | None = None,
            **filters: Any,
        ) -> list[Record]:
            captured_calls.append(
                {"study_key": study_key, "record_data_filter": record_data_filter, **filters}
            )
            return list(all_records)

        def get_subjects(self, study_key: str, **filters: Any) -> list[Subject]:
            return [
                Subject(study_key=study_key, subject_key="SUBJ-1"),
                Subject(study_key=study_key, subject_key="SUBJ-2"),
                Subject(study_key=study_key, subject_key="SUBJ-3"),
            ]

        def get_visits(self, study_key: str, **filters: Any) -> list[Visit]:
            return []

    sync_wf = DataExtractionWorkflow(_MinimalSyncFacade())  # type: ignore[arg-type]
    dedup_res = sync_wf.extract_records_by_criteria(
        "STUDY_ENC",
        record_filter={
            "form_key": "DM",
            "record_data_filter": "AGE>=20",
            "recordDataFilter": "OLD",
        },
        record_data_filter="AGE>=21",
        recordDataFilter="IGNORED_OTHER",
    )
    assert len(dedup_res) == 3
    assert captured_calls[-1]["record_data_filter"] == "AGE>=21"
    assert "recordDataFilter" not in captured_calls[-1]

    # Fallback in iter_records_by_criteria when sdk lacks .records.list
    streamed_fallback = list(
        sync_wf.iter_records_by_criteria(
            "STUDY_ENC",
            subject_filter={"subject_status": "Enrolled"},
            record_data_filter="AGE>=21",
        )
    )
    assert [r.record_id for r in streamed_fallback] == [1, 2, 3]

    # Async workflow also deduplicates record_data_filter cleanly
    async_wf = AsyncDataExtractionWorkflow(_MinimalSyncFacade())  # type: ignore[arg-type]
    async_res = await async_wf.extract_records_by_criteria(
        "STUDY_ENC",
        record_filter={"recordDataFilter": "AGE>=18"},
        record_data_filter="AGE>=22",
    )
    assert len(async_res) == 3
    assert captured_calls[-1]["record_data_filter"] == "AGE>=22"

    # 2. CLI workflows extract-records --record-data-filter, --limit, --all, and [a] lazy paging
    sdk = _setup_encrypted_store_sdk(
        monkeypatch,
        tmp_path,
        study_key="STUDY_ENC",
        phi_fields=["patientAlias"],
    )

    page_fetches: list[int] = []

    class _FakePagedStream:
        def __init__(self, *, size: int | None = None, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.size = size or 100
            self._idx = 0

        @property
        def has_more(self) -> bool:
            return self._idx < len(all_records)

        def __iter__(self) -> _FakePagedStream:
            return self

        def __next__(self) -> Record:
            if self._idx >= len(all_records):
                raise StopIteration
            if self._idx % self.size == 0:
                page_fetches.append(self._idx // self.size)
            rec = all_records[self._idx]
            self._idx += 1
            return rec

    list_calls: list[dict[str, Any]] = []

    def _fake_records_list(study_key: str | None = None, **kwargs: Any) -> _FakePagedStream:
        list_calls.append({"study_key": study_key, **kwargs})
        return _FakePagedStream(**kwargs)

    sdk.records = MagicMock()
    sdk.records.list.side_effect = _fake_records_list
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    b_tok = set_base_url_context(None)
    s_tok = set_security_key_context(None)
    try:
        captured_grid: dict[str, Any] = {}
        with (
            patch("imednet.cli.utils.grid.is_interactive_terminal", return_value=True),
            patch(
                "imednet.cli.utils.grid.run_interactive_grid",
                side_effect=lambda **kw: captured_grid.update(kw) or True,
            ),
        ):
            code_lim, _, err_lim = _invoke_cli(
                [
                    "workflows",
                    "extract-records",
                    "STUDY_ENC",
                    "--record-data-filter",
                    "AGE>=21",
                    "--limit",
                    "2",
                ],
                capsys,
            )
        assert code_lim == 0, err_lim
        assert list_calls[-1]["record_data_filter"] == "AGE>=21"
        assert list_calls[-1]["size"] == 2
        # Only Page 0 fetched before pressing [a]!
        assert page_fetches == [0]
        assert captured_grid["truncated"] is True
        assert [row["ID"] for row in captured_grid["items"]] == [1, 2]
        assert captured_grid["details"][0].record_data["patientAlias"] == MASKED_SENTINEL

        # Pressing [a] drains Page 1 and masks PHI on newly loaded records
        more_rows, more_details = captured_grid["on_load_all"]()
        assert page_fetches == [0, 1]
        assert [row["ID"] for row in more_rows] == [3]
        assert more_details[0].record_data["patientAlias"] == MASKED_SENTINEL

        # --all in TTY mode fetches all records immediately without truncation
        page_fetches.clear()
        captured_all_grid: dict[str, Any] = {}
        with (
            patch("imednet.cli.utils.grid.is_interactive_terminal", return_value=True),
            patch(
                "imednet.cli.utils.grid.run_interactive_grid",
                side_effect=lambda **kw: captured_all_grid.update(kw) or True,
            ),
        ):
            code_all, _, err_all = _invoke_cli(
                [
                    "workflows",
                    "extract-records",
                    "STUDY_ENC",
                    "--record-data-filter",
                    "AGE>=21",
                    "--all",
                ],
                capsys,
            )
        assert code_all == 0, err_all
        assert captured_all_grid.get("truncated", False) is False
        assert "on_load_all" not in captured_all_grid
        assert [row["ID"] for row in captured_all_grid["items"]] == [1, 2, 3]
    finally:
        set_security_key_context(s_tok)
        set_base_url_context(b_tok)
