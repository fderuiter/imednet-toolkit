"""Unit tests for uat engine."""

from unittest.mock import Mock

from imednet.datadict.models import DataDictionary
from imednet.errors import ValidationError
from imednet_workflows.uat.engine import EditCheckResultStatus, UATExecutionEngine


def test_uat_engine_parses_rules():
    """Test that uat engine parses rules."""
    dd = DataDictionary(
        business_logic=[
            {
                "Name": "Rule1",
                "Status": "Active",
                "Form": "F1",
                "Variable": "V1",
                "Operator": ">=",
                "Value": "18",
            },
            {"Name": "Rule2", "Status": "Inactive", "Form": "F2"},
        ],
        choices=[],
        forms=[],
        questions=[],
    )
    engine = UATExecutionEngine(Mock(), dd)
    assert len(engine._rules) == 1
    assert engine._rules[0]["rule_name"] == "Rule1"


def test_uat_engine_negative_generation():
    """Test that uat engine negative generation."""
    dd = DataDictionary(
        business_logic=[
            {
                "Name": "Rule1",
                "Status": "Active",
                "Form": "F1",
                "Variable": "V1",
                "Operator": ">=",
                "Value": "18",
            }
        ],
        choices=[],
        forms=[],
        questions=[],
    )
    engine = UATExecutionEngine(Mock(), dd)
    data = engine.generate_negative_test_case(engine._rules[0])
    assert data["V1"] == "17.0"


def test_uat_engine_run_verification():
    """Test that uat engine run verification."""
    sdk = Mock()
    sdk.create_record.side_effect = ValidationError("Invalid value")

    dd = DataDictionary(
        business_logic=[
            {
                "Name": "Rule1",
                "Status": "Active",
                "Form": "F1",
                "Variable": "V1",
                "Operator": ">=",
                "Value": "18",
            }
        ],
        choices=[],
        forms=[],
        questions=[],
    )
    engine = UATExecutionEngine(sdk, dd)
    report = engine.run_verification("STUDY1")

    assert report.total_rules == 1
    assert report.passed_rules == 1
    assert report.results[0]["status"] == EditCheckResultStatus.PASS


def test_uat_engine_subject_limit():
    """Test that uat engine subject limit."""
    sdk = Mock()
    sdk.create_record.side_effect = ValidationError("Invalid value")

    rules = []
    for i in range(150):
        rules.append(
            {
                "Name": f"Rule{i}",
                "Status": "Active",
                "Form": "F1",
                "Variable": f"V{i}",
                "Operator": ">=",
                "Value": "18",
            }
        )

    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    engine = UATExecutionEngine(sdk, dd)
    report = engine.run_verification("STUDY1")

    assert report.total_rules == 100
    assert report.passed_rules == 100
    assert len(report.results) == 100


def test_generate_negative_test_case_all_branches():
    from imednet_workflows.uat.engine import UATExecutionEngine

    engine = UATExecutionEngine(Mock(), Mock(business_logic=[]))

    # Missing variable
    assert engine.generate_negative_test_case({}) == {}

    # Dependency branches
    assert engine.generate_negative_test_case(
        {"variable": "V1", "dependency_variable": "D1", "dependency_value": "Yes", "operator": "=="}
    ) == {"D1": "Yes", "V1": "NOT_None"}
    assert engine.generate_negative_test_case(
        {
            "variable": "V1",
            "dependency_variable": "D1",
            "dependency_value": "Yes",
            "operator": "!=",
            "value": "A",
        }
    ) == {"D1": "Yes", "V1": "A"}
    assert engine.generate_negative_test_case(
        {"variable": "V1", "dependency_variable": "D1", "dependency_value": "Yes", "operator": ">="}
    ) == {"D1": "Yes", "V1": "Invalid_Value"}

    # Non-dependency branches
    assert engine.generate_negative_test_case(
        {"variable": "V1", "operator": "<=", "value": "10"}
    ) == {"V1": "11.0"}
    assert engine.generate_negative_test_case(
        {"variable": "V1", "operator": "<=", "value": "BAD"}
    ) == {"V1": "9999"}
    assert engine.generate_negative_test_case(
        {"variable": "V1", "operator": ">=", "value": "BAD"}
    ) == {"V1": "0"}
    assert engine.generate_negative_test_case(
        {"variable": "V1", "operator": "==", "value": "X"}
    ) == {"V1": "NOT_X"}
    assert engine.generate_negative_test_case(
        {"variable": "V1", "operator": "!=", "value": "X"}
    ) == {"V1": "X"}
    assert engine.generate_negative_test_case({"variable": "V1", "operator": "Required"}) == {
        "V1": ""
    }
    assert engine.generate_negative_test_case({"variable": "V1", "operator": "OTHER"}) == {
        "V1": "Invalid_Test_Value"
    }


def test_uat_engine_run_verification_failure_and_skip():
    from imednet.datadict.models import DataDictionary
    from imednet.errors import ApiError
    from imednet_workflows.uat.engine import UATExecutionEngine

    sdk = Mock()
    # Mock create_record to return success instead of ValidationError, meaning the edit check failed
    sdk.create_record.return_value = Mock()

    dd = DataDictionary(
        business_logic=[
            {
                "Name": "Rule1",
                "Status": "Active",
                "Form": "F1",
                "Variable": "V1",
                "Operator": ">=",
                "Value": "18",
            },
            {
                "Name": "RuleSkip",
                "Status": "Active",
                "Form": "F1",
            },
            {
                "Name": "RuleError",
                "Status": "Active",
                "Form": "F1",
                "Variable": "V2",
                "Operator": ">=",
                "Value": "18",
            },
        ],
        choices=[],
        forms=[],
        questions=[],
    )
    engine = UATExecutionEngine(sdk, dd)

    # For RuleError, raise a generic API error to test that branch
    def side_effect(*args, **kwargs):
        payloads = args[1] if len(args) > 1 else []
        if any('V2' in p.get('data', {}) for p in payloads):
            raise ApiError("Some other API error")
        return Mock()

    sdk.create_record.side_effect = side_effect

    report = engine.run_verification("STUDY1")

    assert report.total_rules == 3
    assert report.failed_rules == 1
    assert report.skipped_rules == 1


def test_uat_engine_parses_and_verifies_xml_business_logic_rules():
    """UATExecutionEngine parses single and multi-condition XML BusinessLogicRules and verifies them."""
    from imednet.datadict import (
        BusinessLogicRule,
        Choice,
        DataDictionary,
        Form,
        Variable,
        parse_logic_xml,
    )
    from imednet.validation import validate_record_entry

    dd = DataDictionary(
        forms=[
            Form(
                formKey="F_DM",
                formName="Demographics",
                formId=101,
                variables=[
                    Variable(variableName="AGE", variableType="Integer", required="Yes"),
                    Variable(
                        variableName="SEX",
                        variableType="Radio",
                        required="Yes",
                        choices=[
                            Choice(choiceText="Male", choiceValue="M"),
                            Choice(choiceText="Female", choiceValue="F"),
                        ],
                    ),
                    Variable(
                        variableName="PREG",
                        variableType="Single Select",
                        choices=[
                            Choice(choiceText="Yes", choiceValue="Yes"),
                            Choice(choiceText="No", choiceValue="No"),
                        ],
                    ),
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="XML_1",
                        name="Underage Check",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>AGE</variable><operator>&gt;=</operator><value>18</value><false/>"
                            "</condition></conditions>"
                            "<actions><action><type>error</type><message>Must be >= 18</message></action></actions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        id="XML_2",
                        name="Male Pregnancy Check",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions>"
                            "<condition><variable>SEX</variable><operator>==</operator><value>M</value><true/></condition>"
                            "<condition><variable>PREG</variable><operator>==</operator><value>Yes</value><true/></condition>"
                            "</conditions>"
                            "<actions><action><type>error</type><message>Invalid pregnancy</message></action></actions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        id="XML_UI",
                        name="Disable PREG",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition><variable>SEX</variable><operator>==</operator><value>M</value></condition></conditions>"
                            "<actions><action><type>disable</type></action></actions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        id="XML_CROSS",
                        name="Cross Form Check",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition><variable>OTHER.VAR</variable><operator>==</operator><value>1</value></condition></conditions></rule>"
                        ),
                    ),
                ],
            )
        ]
    )

    sdk = Mock()

    def _validate_and_reject(study_key, payloads):
        for payload in payloads:
            validate_record_entry(dd, payload)
        return Mock()

    sdk.create_record.side_effect = _validate_and_reject

    engine = UATExecutionEngine(sdk, dd)
    # UI-only and cross-form XML rules are skipped; both active single-Form rules are parsed
    assert len(engine._rules) == 2
    assert engine._rules[0]["rule_name"] == "Underage Check"
    assert engine._rules[0]["form"] == "F_DM"
    assert engine._rules[0]["variable"] == "AGE"

    assert engine._rules[1]["rule_name"] == "Male Pregnancy Check"
    assert engine._rules[1]["variable"] == "PREG"
    assert engine._rules[1]["dependency_variable"] == "SEX"
    assert engine._rules[1]["dependency_value"] == "M"

    report = engine.run_verification("STUDY1")
    assert report.total_rules == 2
    assert report.passed_rules == 2
    assert report.failed_rules == 0
    assert all(r["status"] == EditCheckResultStatus.PASS for r in report.results)


def test_xml_negative_synthesis_operators_and_required_defaults():
    """UATExecutionEngine synthesizes negative payloads across XML operators and populates required fields."""
    from imednet.datadict import (
        BusinessLogicRule,
        Choice,
        DataDictionary,
        Form,
        Variable,
        parse_logic_xml,
    )

    dd = DataDictionary(
        forms=[
            Form(
                formKey="VS",
                variables=[
                    Variable(variableName="SYSBP", variableType="Integer", required="Yes"),
                    Variable(variableName="DIABP", variableType="Integer", required="Yes"),
                    Variable(variableName="TEMP", variableType="Float", required="Yes"),
                    Variable(
                        variableName="POS",
                        variableType="Coded",
                        required="Yes",
                        choices=[
                            Choice(choiceText="Sitting", choiceValue="SIT"),
                            Choice(choiceText="Standing", choiceValue="STAND"),
                        ],
                    ),
                    Variable(variableName="NOTES", variableType="Text", required="No"),
                ],
                businessLogic=[
                    BusinessLogicRule(
                        name="Compare BP",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>DIABP</variable><operator>&gt;=</operator>"
                            "<compareVariable>SYSBP</compareVariable>"
                            "</condition></conditions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        name="Empty Notes",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>NOTES</variable><empty/>"
                            "</condition></conditions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        name="Not Empty Notes",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>NOTES</variable><notEmpty/>"
                            "</condition></conditions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        name="Not Equal Pos",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>POS</variable><operator>!=</operator><value>SIT</value>"
                            "</condition></conditions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        name="In Set",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>POS</variable><operator>in</operator><value>SIT,STAND</value>"
                            "</condition></conditions></rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        name="Contains Text",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions operator='OR'>"
                            "<condition><variable>TEMP</variable><operator>&gt;</operator><value>41.0</value></condition>"
                            "<condition><variable>NOTES</variable><operator>contains</operator><value>ERR</value></condition>"
                            "</conditions></rule>"
                        ),
                    ),
                ],
            )
        ]
    )

    engine = UATExecutionEngine(Mock(), dd)
    assert len(engine._rules) == 6
    payloads = [engine.generate_negative_test_case(r) for r in engine._rules]
    assert payloads[0]["DIABP"] > payloads[0]["SYSBP"]
    assert payloads[1]["NOTES"] == ""
    assert payloads[2]["NOTES"] != ""
    assert payloads[3]["POS"] == "STAND"
    assert payloads[4]["POS"] == "SIT"
    assert payloads[5]["NOTES"] == "ERR"
    assert isinstance(payloads[5]["TEMP"], float)


def test_uat_engine_explicit_or_group_xml_rule() -> None:
    """UATExecutionEngine extracts logical_operator='OR' from grouped <or> XML rules and synthesizes triggering payloads."""
    from imednet.datadict import (
        BusinessLogicRule,
        DataDictionary,
        Form,
        LogicTreeEvaluator,
        Variable,
        parse_logic_xml,
    )

    or_rule = BusinessLogicRule(
        id="HR-OR",
        name="Abnormal Heart Rate",
        type="Form",
        status="Active",
        form="VS",
        logic=parse_logic_xml(
            "<rule><conditions><or>"
            "<condition><variable>HR</variable><operator>&lt;</operator><value>40</value></condition>"
            "<condition><variable>HR</variable><operator>&gt;</operator><value>120</value></condition>"
            "</or></conditions></rule>"
        ),
    )
    dd = DataDictionary(
        forms=[
            Form(
                formKey="VS",
                variables=[Variable(variableName="HR", variableType="Integer")],
                businessLogic=[or_rule],
            )
        ]
    )

    engine = UATExecutionEngine(Mock(), dd)
    assert len(engine._rules) == 1
    assert engine._rules[0]["logical_operator"] == "OR"

    neg_payload = engine.generate_negative_test_case(engine._rules[0])
    assert neg_payload["HR"] > 120
    assert LogicTreeEvaluator().evaluate_rule(or_rule, neg_payload, form_key="VS").triggered is True

    dnf_rule = BusinessLogicRule(
        id="VS-DNF",
        name="Critical Vital Pair",
        type="Form",
        status="Active",
        form="VS",
        logic=parse_logic_xml(
            "<rule><conditions><or>"
            "<and>"
            "<condition><variable>SYS</variable><operator>&gt;</operator><value>200</value></condition>"
            "<condition><variable>HR</variable><operator>&gt;</operator><value>120</value></condition>"
            "</and>"
            "<and>"
            "<condition><variable>DIA</variable><operator>&lt;</operator><value>40</value></condition>"
            "<condition><variable>HR</variable><operator>&lt;</operator><value>50</value></condition>"
            "</and>"
            "</or></conditions></rule>"
        ),
    )
    dnf_dd = DataDictionary(
        forms=[
            Form(
                formKey="VS",
                variables=[
                    Variable(variableName="SYS", variableType="Integer"),
                    Variable(variableName="DIA", variableType="Integer"),
                    Variable(variableName="HR", variableType="Integer"),
                ],
                businessLogic=[dnf_rule],
            )
        ]
    )
    dnf_engine = UATExecutionEngine(Mock(), dnf_dd)
    assert dnf_engine._rules[0]["logical_operator"] == "OR"
    dnf_payload = dnf_engine.generate_negative_test_case(dnf_engine._rules[0])
    assert (
        LogicTreeEvaluator().evaluate_rule(dnf_rule, dnf_payload, form_key="VS").triggered is True
    )


def test_uat_engine_async_job_polling_classifications():
    """UATExecutionEngine polls HTTP 202 Job batch_ids concurrently and classifies outcomes."""
    from unittest.mock import patch

    from imednet.errors import ApiError, ClientError, ServerError
    from imednet.spi.models import Job, JobStatus
    from imednet.spi.utils import JobPoller, JobTimeoutError

    rules = [
        {
            "Name": "AsyncFailedState",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V1",
            "Operator": ">=",
            "Value": "18",
        },
        {
            "Name": "AsyncFailedRecords",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V2",
            "Operator": ">=",
            "Value": "18",
        },
        {
            "Name": "AsyncAccepted",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V3",
            "Operator": ">=",
            "Value": "18",
        },
        {
            "Name": "AsyncTimeout",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V4",
            "Operator": ">=",
            "Value": "18",
        },
        {
            "Name": "SyncClientReject",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V5",
            "Operator": ">=",
            "Value": "18",
        },
        {
            "Name": "SyncServerErr",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V6",
            "Operator": ">=",
            "Value": "18",
        },
        {
            "Name": "SyncApi500Err",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V7",
            "Operator": ">=",
            "Value": "18",
        },
    ]

    sdk = Mock()

    def create_record_side_effect(study_key, payloads):
        data = payloads[0]["data"]
        if "V1" in data:
            return Job(batch_id="job-failed-state", state="PENDING")
        if "V2" in data:
            return Job(batch_id="job-failed-records", state="PENDING")
        if "V3" in data:
            return Job(batch_id="job-clean-completed", state="PENDING")
        if "V4" in data:
            return Job(batch_id="job-timed-out", state="PENDING")
        if "V5" in data:
            raise ClientError("HTTP 400 validation error", status_code=400)
        if "V6" in data:
            raise ServerError("HTTP 502 bad gateway", status_code=502)
        raise ApiError("HTTP 500 internal error", status_code=500)

    def get_job_side_effect(study_key, batch_id):
        if batch_id == "job-failed-state":
            return JobStatus(batch_id=batch_id, state="FAILED")
        if batch_id == "job-failed-records":
            return JobStatus(batch_id=batch_id, state="COMPLETED", failed_records=1)
        if batch_id == "job-clean-completed":
            return JobStatus(batch_id=batch_id, state="COMPLETED", failed_records=0)
        raise JobTimeoutError("Polling timed out")

    sdk.create_record.side_effect = create_record_side_effect
    sdk.get_job.side_effect = get_job_side_effect

    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    engine = UATExecutionEngine(sdk, dd, poll_interval=0.01, poll_timeout=5.0)

    with patch.object(JobPoller, "poll_many", autospec=True, wraps=JobPoller.poll_many) as spy_poll:
        report = engine.run_verification("STUDY1")

    spy_poll.assert_called_once()
    polled_batch_ids = spy_poll.call_args.args[2]
    assert polled_batch_ids == [
        "job-failed-state",
        "job-failed-records",
        "job-clean-completed",
        "job-timed-out",
    ]
    assert spy_poll.call_args.kwargs["fail_fast"] is False

    status_by_rule = {entry["rule_name"]: entry["status"] for entry in report.results}
    assert status_by_rule == {
        "AsyncFailedState": EditCheckResultStatus.PASS,
        "AsyncFailedRecords": EditCheckResultStatus.PASS,
        "AsyncAccepted": EditCheckResultStatus.FAIL,
        "AsyncTimeout": EditCheckResultStatus.ERROR,
        "SyncClientReject": EditCheckResultStatus.PASS,
        "SyncServerErr": EditCheckResultStatus.ERROR,
        "SyncApi500Err": EditCheckResultStatus.ERROR,
    }
    assert report.total_rules == 7
    assert report.passed_rules == 3
    assert report.failed_rules == 4


def test_uat_engine_async_job_polling_enforces_100_rule_cap():
    """UATExecutionEngine caps submissions at 100 active rules before polling batch_ids."""
    from unittest.mock import patch

    from imednet.spi.models import Job, JobStatus
    from imednet.spi.utils import JobPoller

    rules = [
        {
            "Name": f"Rule{i}",
            "Status": "Active",
            "Form": "F1",
            "Variable": f"V{i}",
            "Operator": ">=",
            "Value": "18",
        }
        for i in range(135)
    ]
    sdk = Mock()
    sdk.create_record.side_effect = [
        Job(batch_id=f"batch-{i}", state="PENDING") for i in range(135)
    ]
    sdk.get_job.side_effect = lambda _study, bid: JobStatus(
        batch_id=bid, state="FAILED", failed_records=1
    )

    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    engine = UATExecutionEngine(sdk, dd, poll_interval=0.01, poll_timeout=5.0)

    with patch.object(JobPoller, "poll_many", autospec=True, wraps=JobPoller.poll_many) as spy_poll:
        report = engine.run_verification("STUDY1")

    assert sdk.create_record.call_count == 100
    spy_poll.assert_called_once()
    assert len(spy_poll.call_args.args[2]) == 100
    assert report.total_rules == 100
    assert report.passed_rules == 100
    assert report.failed_rules == 0


def test_uat_engine_resolves_site_interval_and_registers_or_reuses_uat_subject():
    """UATExecutionEngine inspects metadata, resolves open Site and active Interval, and reuses/registers one UAT Subject."""
    import pytest

    from imednet.datadict import DataDictionary
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVariable
    from imednet.models.forms import Form as APIForm
    from imednet.models.intervals import Interval
    from imednet.models.sites import Site
    from imednet.models.subjects import Subject, SubjectKeyword
    from imednet.models.variables import Variable as APIVariable
    from imednet.spi.models import Job, JobStatus

    dd = DataDictionary(
        business_logic=[
            {
                "Name": "BP_Rule",
                "Status": "Active",
                "Form": "F_VS",
                "Variable": "SYSBP",
                "Operator": ">=",
                "Value": "80",
            },
            {
                "Name": "HR_Rule",
                "Status": "Active",
                "Form": "F_VS",
                "Variable": "HR",
                "Operator": ">=",
                "Value": "40",
            },
            {
                "Name": "AE_Rule",
                "Status": "Active",
                "Form": "F_AE",
                "Variable": "SEV",
                "Operator": "==",
                "Value": "Mild",
            },
        ],
        forms=[
            DDForm(
                formKey="F_DM",
                variables=[DDVariable(variableName="AGE", variableType="Integer", required="Yes")],
            ),
            DDForm(
                formKey="F_VS",
                variables=[
                    DDVariable(variableName="SYSBP", variableType="Integer"),
                    DDVariable(variableName="HR", variableType="Integer"),
                ],
            ),
            DDForm(
                formKey="F_AE",
                variables=[DDVariable(variableName="SEV", variableType="Text")],
            ),
        ],
    )

    forms = [
        APIForm(
            form_key="F_DM",
            form_name="Demographics",
            form_type="Subject",
            subject_record_report=True,
        ),
        APIForm(form_key="F_VS", form_name="Vital Signs", form_type="Subject"),
        APIForm(
            form_key="F_AE",
            form_name="Adverse Events",
            form_type="Subject",
            unscheduled_visit=True,
        ),
    ]
    intervals = [
        Interval(
            interval_name="DisabledVisit",
            disabled=True,
            forms=[{"form_id": 2, "form_key": "F_VS", "form_name": "Vital Signs"}],
        ),
        Interval(
            interval_name="ActiveVisit1",
            disabled=False,
            forms=[{"form_id": 2, "form_key": "F_VS", "form_name": "Vital Signs"}],
        ),
    ]

    # 1. Closed site raises ValueError
    sdk_closed = Mock()
    sdk_closed.get_forms.return_value = forms
    sdk_closed.get_variables.return_value = [
        APIVariable(form_key="F_DM", variable_name="AGE", variable_type="Number")
    ]
    sdk_closed.get_intervals.return_value = intervals
    sdk_closed.get_sites.return_value = [
        Site(site_name="ClosedSite", site_enrollment_status="Closed")
    ]
    engine_closed = UATExecutionEngine(sdk_closed, dd)
    with pytest.raises(ValueError, match="open sites"):
        engine_closed.run_verification("STUDY1")
    with pytest.raises(ValueError, match="not eligible"):
        engine_closed.run_verification("STUDY1", site_name="ClosedSite")

    # 2. No UAT subject exists yet -> registers 1 UAT subject and reuses it across all 3 rules
    sdk_reg = Mock()
    sdk_reg.get_forms.return_value = forms
    sdk_reg.get_variables.return_value = [
        APIVariable(form_key="F_DM", variable_name="AGE", variable_type="Number")
    ]
    sdk_reg.get_intervals.return_value = intervals
    sdk_reg.get_sites.return_value = [Site(site_name="OpenSite", site_enrollment_status="Active")]
    sdk_reg.get_subjects.return_value = []
    submitted_payloads: list[dict] = []

    def create_record_reg_then_rules(_study_key, payloads):
        submitted_payloads.append(payloads[0])
        idx = len(submitted_payloads)
        if idx == 1:
            return Job(batch_id="reg-batch", state="PENDING")
        return Job(batch_id=f"rule-batch-{idx - 1}", state="PENDING")

    def get_job_reg_then_rules(_study_key, batch_id):
        if batch_id == "reg-batch":
            return JobStatus(
                batch_id=batch_id,
                state="COMPLETED",
                results=[{"subjectKey": "SUBJ-ENROLLED-1", "status": "SUCCESS"}],
            )
        return JobStatus(batch_id=batch_id, state="COMPLETED", failed_records=1)

    sdk_reg.create_record.side_effect = create_record_reg_then_rules
    sdk_reg.get_job.side_effect = get_job_reg_then_rules
    sdk_reg.poll_jobs = None

    report_reg = UATExecutionEngine(sdk_reg, dd, poll_interval=0.01).run_verification(
        "STUDY1", site_name="OpenSite"
    )
    assert report_reg.total_rules == 3
    assert report_reg.passed_rules == 3
    assert len(submitted_payloads) == 4  # 1 prerequisite registration + 3 rules
    assert submitted_payloads[0]["formKey"] == "F_DM"
    assert submitted_payloads[0]["siteName"] == "OpenSite"
    assert submitted_payloads[0]["keywords"] == [{"keywordName": "UAT", "keywordKey": "UAT"}]

    # Scheduled form payloads include enrolled subjectKey, siteName, and active intervalName
    assert submitted_payloads[1]["subjectKey"] == "SUBJ-ENROLLED-1"
    assert submitted_payloads[1]["siteName"] == "OpenSite"
    assert submitted_payloads[1]["intervalName"] == "ActiveVisit1"
    assert submitted_payloads[2]["subjectKey"] == "SUBJ-ENROLLED-1"
    assert submitted_payloads[2]["intervalName"] == "ActiveVisit1"
    # Unscheduled form omits intervalName while keeping enrolled subjectKey and siteName
    assert submitted_payloads[3]["subjectKey"] == "SUBJ-ENROLLED-1"
    assert submitted_payloads[3]["siteName"] == "OpenSite"
    assert "intervalName" not in submitted_payloads[3]

    # 3. Existing UAT subject at site_name is reused with 0 registrations
    sdk_reuse = Mock()
    sdk_reuse.get_forms.return_value = forms
    sdk_reuse.get_variables.return_value = []
    sdk_reuse.get_intervals.return_value = intervals
    sdk_reuse.get_sites.return_value = [Site(site_name="OpenSite", site_enrollment_status="Active")]
    sdk_reuse.get_subjects.return_value = [
        Subject(
            subject_key="SUBJ-REUSED-99",
            site_name="OpenSite",
            deleted=False,
            keywords=[SubjectKeyword(keyword_key="UAT", keyword_name="UAT")],
        )
    ]
    reused_payloads: list[dict] = []

    def create_record_reuse(_study_key, payloads):
        reused_payloads.append(payloads[0])
        return Job(batch_id=f"r-{len(reused_payloads)}", state="PENDING")

    sdk_reuse.create_record.side_effect = create_record_reuse
    sdk_reuse.get_job.side_effect = lambda _s, bid: JobStatus(
        batch_id=bid, state="FAILED", failed_records=1
    )
    sdk_reuse.poll_jobs = None

    report_reuse = UATExecutionEngine(sdk_reuse, dd, poll_interval=0.01).run_verification(
        "STUDY1", site_name="OpenSite"
    )
    assert report_reuse.passed_rules == 3
    assert len(reused_payloads) == 3
    assert all(p["subjectKey"] == "SUBJ-REUSED-99" for p in reused_payloads)


def test_uat_engine_classifies_http_401_403_404_409_429_and_preserves_diagnostics():
    """AuthenticationError, AuthorizationError, NotFoundError, ConflictError, and RateLimitError report ERROR."""
    import httpx
    import respx

    from imednet import ImednetSDK
    from imednet.spi.errors import (
        AuthenticationError,
        AuthorizationError,
        ConflictError,
        DiagnosticContext,
        NotFoundError,
        RateLimitError,
    )

    rules = [
        {
            "Name": f"Rule_{code}",
            "Status": "Active",
            "Form": "F1",
            "Variable": f"V_{code}",
            "Operator": ">=",
            "Value": "18",
        }
        for code in (401, 403, 404, 409, 429, 400)
    ]
    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])

    diag = DiagnosticContext(
        method="POST",
        url="https://api.test/api/v1/edc/studies/STUDY1/records",
        retry_after=12.0,
        attempt_count=2,
    )
    err_map = {
        "V_401": AuthenticationError(
            "Unauthorized key",
            status_code=401,
            diagnostic_context=diag,
        ),
        "V_403": AuthorizationError("Forbidden study", status_code=403),
        "V_404": NotFoundError("Study not found", status_code=404),
        "V_409": ConflictError("Record lock conflict", status_code=409),
        "V_429": RateLimitError("Too many requests", status_code=429),
        "V_400": ValidationError("Value below minimum"),
    }

    sdk = Mock()

    def create_side_effect(_study_key, payloads):
        var_name = next(iter(payloads[0]["data"].keys()))
        raise err_map[var_name]

    sdk.create_record.side_effect = create_side_effect
    report = UATExecutionEngine(sdk, dd).run_verification("STUDY1")

    by_rule = {r["rule_name"]: r for r in report.results}
    for code in (401, 403, 404, 409, 429):
        assert by_rule[f"Rule_{code}"]["status"] == EditCheckResultStatus.ERROR
    assert by_rule["Rule_401"]["diagnostic_context"] == diag
    assert "HTTP 401" in by_rule["Rule_401"]["actual_response"]
    assert (
        "https://api.test/api/v1/edc/studies/STUDY1/records"
        in by_rule["Rule_401"]["actual_response"]
    )
    assert by_rule["Rule_400"]["status"] == EditCheckResultStatus.PASS
    assert report.passed_rules == 1
    assert report.failed_rules == 5

    # Also verify real HTTP 401/403/404/429 responses via ImednetSDK mark ERROR instead of crashing
    single_dd = DataDictionary(business_logic=[rules[0]], choices=[], forms=[], questions=[])
    for http_code in (401, 403, 404, 429):
        with respx.mock(assert_all_called=False) as router:
            router.route().mock(return_value=httpx.Response(http_code, json={"message": "err"}))
            real_sdk = ImednetSDK(
                api_key="k",
                security_key="s",
                base_url="https://api.test",
                retries=0,
            )
            http_report = UATExecutionEngine(real_sdk, single_dd).run_verification("STUDY1")
            assert http_report.total_rules == 1
            assert http_report.results[0]["status"] == EditCheckResultStatus.ERROR


def test_uat_engine_classifies_cancelled_and_structural_job_errors_as_error():
    """CANCELLED jobs and structural Subject/Site/Interval errors in JobStatus.results report ERROR."""
    from imednet.spi.models import Job, JobStatus

    rules = [
        {
            "Name": name,
            "Status": "Active",
            "Form": "F1",
            "Variable": f"V_{i}",
            "Operator": ">=",
            "Value": "18",
        }
        for i, name in enumerate(
            [
                "CancelledJob",
                "UnknownSubjectFailed",
                "MissingIntervalCompleted",
                "UnknownSiteCompleted",
                "LegitEditCheckReject",
            ],
            start=1,
        )
    ]
    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    sdk = Mock()
    sdk.create_record.side_effect = [
        Job(batch_id="b-cancel", state="PENDING"),
        Job(batch_id="b-unk-subj", state="PENDING"),
        Job(batch_id="b-miss-int", state="PENDING"),
        Job(batch_id="b-unk-site", state="PENDING"),
        Job(batch_id="b-edit-check", state="PENDING"),
    ]
    job_statuses = {
        "b-cancel": JobStatus(batch_id="b-cancel", state="CANCELLED"),
        "b-unk-subj": JobStatus(
            batch_id="b-unk-subj",
            state="FAILED",
            failed_records=1,
            results=[{"error": "Subject UAT-SUBJ-0 not found in study"}],
        ),
        "b-miss-int": JobStatus(
            batch_id="b-miss-int",
            state="COMPLETED",
            failed_records=1,
            results=[{"message": "Missing required intervalName for scheduled form F1"}],
        ),
        "b-unk-site": JobStatus(
            batch_id="b-unk-site",
            state="COMPLETED",
            failed_records=1,
            results={"records": [{"errors": ["Unknown site TestSite"]}]},
        ),
        "b-edit-check": JobStatus(
            batch_id="b-edit-check",
            state="COMPLETED",
            failed_records=1,
            results=[{"error": "Edit check violation: V_5 must be >= 18"}],
        ),
    }
    sdk.get_job.side_effect = lambda _study, bid: job_statuses[bid]
    sdk.poll_jobs = None

    report = UATExecutionEngine(sdk, dd, poll_interval=0.01).run_verification("STUDY1")
    statuses = {r["rule_name"]: r["status"] for r in report.results}
    assert statuses == {
        "CancelledJob": EditCheckResultStatus.ERROR,
        "UnknownSubjectFailed": EditCheckResultStatus.ERROR,
        "MissingIntervalCompleted": EditCheckResultStatus.ERROR,
        "UnknownSiteCompleted": EditCheckResultStatus.ERROR,
        "LegitEditCheckReject": EditCheckResultStatus.PASS,
    }
    assert report.passed_rules == 1
    assert report.failed_rules == 4


def test_uat_engine_verifies_soft_warning_and_query_rules_via_get_queries():
    """Soft warning / Query / Auto Query rules expect 'Query Opened' and check open Queries via sdk.get_queries."""
    from imednet.datadict import (
        BusinessLogicRule,
        DataDictionary,
        Form,
        Variable,
        parse_logic_xml,
    )
    from imednet.models.queries import Query, QueryComment
    from imednet.spi.models import Job, JobStatus

    dd = DataDictionary(
        business_logic=[
            {
                "Name": "TabularQueryPass",
                "Status": "Active",
                "Form": "F_VS",
                "Variable": "TEMP",
                "Operator": "<=",
                "Value": "38",
                "Check Type": "Query",
            },
            {
                "Name": "TabularAutoQueryClosedFail",
                "Status": "Active",
                "Form": "F_VS",
                "Variable": "RESP",
                "Operator": "<=",
                "Value": "24",
                "Check Type": "Auto Query",
            },
        ],
        forms=[
            Form(
                formKey="F_VS",
                variables=[
                    Variable(variableName="TEMP", variableType="Float"),
                    Variable(variableName="RESP", variableType="Integer"),
                    Variable(variableName="HR", variableType="Integer"),
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="XML_WARN_PASS",
                        name="XMLWarningOpenQuery",
                        type="Form",
                        status="Active",
                        logic=parse_logic_xml(
                            "<rule><conditions><condition>"
                            "<variable>HR</variable><operator>&gt;</operator><value>110</value>"
                            "</condition></conditions>"
                            "<actions><action><type>warning</type><message>High HR</message></action></actions></rule>"
                        ),
                    )
                ],
            )
        ],
    )

    sdk = Mock()
    submitted_by_batch: dict[str, tuple[str, str]] = {}

    def create_soft_record(_study, payloads):
        payload = payloads[0]
        subj = payload["subjectKey"]
        data = payload["data"]
        if "TEMP" in data:
            bid = "b-temp"
            var_name = "TEMP"
        elif "RESP" in data:
            bid = "b-resp"
            var_name = "RESP"
        else:
            bid = "b-hr"
            var_name = "HR"
        submitted_by_batch[bid] = (subj, var_name)
        return Job(batch_id=bid, state="PENDING")

    sdk.create_record.side_effect = create_soft_record
    sdk.get_job.side_effect = lambda _study, bid: JobStatus(
        batch_id=bid,
        state="COMPLETED",
        failed_records=0,
        results=[
            {
                "recordId": {"b-temp": 901, "b-resp": 902, "b-hr": 903}[bid],
                "status": "SUCCESS",
            }
        ],
    )
    sdk.poll_jobs = None

    def get_queries_for_subject(_study, subject_key=None):
        all_queries = [
            Query(
                study_key="STUDY1",
                subject_key=submitted_by_batch["b-temp"][0],
                record_id=901,
                variable="TEMP",
                query_comments=[QueryComment(sequence=1, closed=False, comment="Check temp")],
            ),
            Query(
                study_key="STUDY1",
                subject_key=submitted_by_batch["b-resp"][0],
                record_id=902,
                variable="RESP",
                query_comments=[QueryComment(sequence=1, closed=True, comment="Resolved")],
            ),
            Query(
                study_key="STUDY1",
                subject_key=submitted_by_batch["b-hr"][0],
                record_id=903,
                variable="HR",
                query_comments=[QueryComment(sequence=1, closed=False, comment="High HR warning")],
            ),
        ]
        if subject_key:
            return [q for q in all_queries if q.subject_key == subject_key]
        return all_queries

    sdk.get_queries.side_effect = get_queries_for_subject

    engine = UATExecutionEngine(sdk, dd, poll_interval=0.01)
    report = engine.run_verification("STUDY1")

    assert sdk.get_queries.call_count == 1
    by_name = {r["rule_name"]: r for r in report.results}
    assert all(r["expected_response"] == "Query Opened" for r in report.results)

    assert by_name["TabularQueryPass"]["status"] == EditCheckResultStatus.PASS
    assert by_name["TabularQueryPass"]["actual_response"] == "Query Opened"

    assert by_name["TabularAutoQueryClosedFail"]["status"] == EditCheckResultStatus.FAIL
    assert by_name["TabularAutoQueryClosedFail"]["actual_response"] == "Accepted (No Open Query)"

    assert by_name["XMLWarningOpenQuery"]["status"] == EditCheckResultStatus.PASS
    assert by_name["XMLWarningOpenQuery"]["actual_response"] == "Query Opened"
    assert report.passed_rules == 2
    assert report.failed_rules == 1


def test_uat_engine_parallel_submissions_and_multipage_queries_single_subject():
    """UATExecutionEngine parallelizes non-mock POST /records with contextvars and paginates Queries once at size=500."""
    import contextvars
    import threading
    import time

    import httpx
    import respx

    from imednet import ImednetSDK

    trace_var: contextvars.ContextVar[str] = contextvars.ContextVar("uat_trace", default="unset")
    rules = [
        {
            "Name": f"SoftRule_{i}",
            "Status": "Active",
            "Form": "F_VS",
            "Variable": f"V_{i}",
            "Operator": "<=",
            "Value": "10",
            "Check Type": "Query",
        }
        for i in range(6)
    ]
    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])

    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0
    seen_traces: list[str] = []
    query_requests: list[httpx.Request] = []

    def handle_records(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        import json

        body = json.loads(request.content.decode("utf-8"))
        var_name = next(iter(body[0]["data"].keys()))
        idx = int(var_name.split("_")[1])
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            seen_traces.append(trace_var.get())
        time.sleep(0.03)
        with lock:
            in_flight -= 1
        return httpx.Response(202, json={"batchId": f"batch-{idx}", "state": "PENDING"})

    def handle_job(_request: httpx.Request, batch_id: str) -> httpx.Response:
        idx = int(batch_id.split("-")[1])
        return httpx.Response(
            200,
            json={
                "batchId": batch_id,
                "state": "COMPLETED",
                "failedRecords": 0,
                "results": [{"recordId": 1000 + idx, "status": "SUCCESS"}],
            },
        )

    all_query_items = [
        {
            "annotationId": i + 1,
            "studyKey": "STUDY1",
            "subjectKey": "SUBJ-SHARED-1",
            "recordId": 9000 + i,
            "variable": "NOISE_VAR",
            "status": "CLOSED",
        }
        for i in range(1050)
    ]
    # Place matching open queries on page 0 and page 2 (index > 1000) to exercise multi-page indexing
    all_query_items[5] = {
        "annotationId": 6,
        "studyKey": "STUDY1",
        "subjectKey": "SUBJ-SHARED-1",
        "recordId": 1000,
        "variable": "V_0",
        "status": "OPEN",
    }
    all_query_items[1049] = {
        "annotationId": 1050,
        "studyKey": "STUDY1",
        "subjectKey": "SUBJ-SHARED-1",
        "recordId": 1005,
        "variable": "V_5",
        "status": "OPEN",
    }

    def handle_queries(request: httpx.Request) -> httpx.Response:
        query_requests.append(request)
        page = int(request.url.params.get("page", "0"))
        size = int(request.url.params.get("size", "100"))
        start = page * size
        chunk = all_query_items[start : start + size]
        total_pages = (len(all_query_items) + size - 1) // size
        return httpx.Response(
            200,
            json={
                "data": chunk,
                "pagination": {
                    "currentPage": page,
                    "size": size,
                    "totalPages": total_pages,
                    "totalElements": len(all_query_items),
                },
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.test/api/v1/edc/studies/STUDY1/forms").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"formId": 1, "formKey": "F_VS", "formName": "VS", "unscheduledVisit": True}
                    ],
                    "pagination": {"totalPages": 1},
                },
            )
        )
        router.get("https://api.test/api/v1/edc/studies/STUDY1/variables").mock(
            return_value=httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})
        )
        router.get("https://api.test/api/v1/edc/studies/STUDY1/intervals").mock(
            return_value=httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})
        )
        router.get("https://api.test/api/v1/edc/studies/STUDY1/sites").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"siteId": 1, "siteName": "TestSite", "siteEnrollmentStatus": "Active"}
                    ],
                    "pagination": {"totalPages": 1},
                },
            )
        )
        router.get("https://api.test/api/v1/edc/studies/STUDY1/subjects").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "subjectKey": "SUBJ-SHARED-1",
                            "siteName": "TestSite",
                            "deleted": False,
                            "keywords": [{"keywordKey": "UAT", "keywordName": "UAT"}],
                        }
                    ],
                    "pagination": {"totalPages": 1},
                },
            )
        )
        router.post("https://api.test/api/v1/edc/studies/STUDY1/records").mock(
            side_effect=handle_records
        )
        router.get(
            url__regex=r"^https://api\.test/api/v1/edc/studies/STUDY1/jobs/(?P<batch_id>[^/]+)$"
        ).mock(side_effect=handle_job)
        router.get("https://api.test/api/v1/edc/studies/STUDY1/queries").mock(
            side_effect=handle_queries
        )

        token = trace_var.set("ctx-uat-42")
        try:
            with ImednetSDK(
                api_key="k",
                security_key="s",
                base_url="https://api.test",
                retries=0,
            ) as real_sdk:
                report = UATExecutionEngine(real_sdk, dd, poll_interval=0.01).run_verification(
                    "STUDY1"
                )
        finally:
            trace_var.reset(token)

    assert max_in_flight >= 2
    assert seen_traces == ["ctx-uat-42"] * 6
    # 1,050 queries at PAGE_SIZE=500 -> exactly 3 GET /queries requests, all scoped to SUBJ-SHARED-1
    assert len(query_requests) == 3
    for page_idx, req in enumerate(query_requests):
        assert req.url.params["size"] == "500"
        assert req.url.params["page"] == str(page_idx)
        assert "subjectKey==SUBJ-SHARED-1" in req.url.params["filter"]

    assert [r["rule_name"] for r in report.results] == [f"SoftRule_{i}" for i in range(6)]
    by_name = {r["rule_name"]: r["status"] for r in report.results}
    assert by_name["SoftRule_0"] == EditCheckResultStatus.PASS
    assert by_name["SoftRule_5"] == EditCheckResultStatus.PASS
    for idx in (1, 2, 3, 4):
        assert by_name[f"SoftRule_{idx}"] == EditCheckResultStatus.FAIL
    assert report.passed_rules == 2
    assert report.failed_rules == 4


def test_uat_engine_mixed_sync_and_job_soft_acceptances_and_zero_soft_skip():
    """UATExecutionEngine defers mixed sync and Job soft acceptances into one get_queries call and skips get_queries when zero soft rules are accepted."""
    from imednet.models.queries import Query
    from imednet.models.records import Record
    from imednet.spi.models import Job, JobStatus

    rules = [
        {
            "Name": "SyncDictSoftPass",
            "Status": "Active",
            "Form": "F_DM",
            "Variable": "V_SYNC_DICT",
            "Operator": "<=",
            "Value": "10",
            "Severity": "warning",
        },
        {
            "Name": "SyncModelSoftFail",
            "Status": "Active",
            "Form": "F_VS",
            "Variable": "V_SYNC_REC",
            "Operator": "<=",
            "Value": "10",
            "Severity": "warning",
        },
        {
            "Name": "AsyncJobSoftPass",
            "Status": "Active",
            "Form": "F_VS",
            "Variable": "V_ASYNC_JOB",
            "Operator": "<=",
            "Value": "10",
            "Severity": "warning",
        },
        {
            "Name": "HardAcceptedFail",
            "Status": "Active",
            "Form": "F_VS",
            "Variable": "V_HARD",
            "Operator": ">=",
            "Value": "18",
        },
    ]
    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])

    sdk = Mock()
    sdk.poll_jobs = None

    def create_mixed(_study, payloads):
        data = payloads[0]["data"]
        if "V_SYNC_DICT" in data:
            # Simulate missing subjectKey on enrollment-like payload by mutating payload in test
            payloads[0].pop("subjectKey", None)
            return {"recordId": 501}
        if "V_SYNC_REC" in data:
            return Record(record_id=502)
        if "V_ASYNC_JOB" in data:
            return Job(batch_id="b-async-soft", state="PENDING")
        return {"recordId": 504}

    sdk.create_record.side_effect = create_mixed
    sdk.get_job.return_value = JobStatus(
        batch_id="b-async-soft",
        state="COMPLETED",
        failed_records=0,
        results=[{"recordId": 503, "status": "SUCCESS"}],
    )
    sdk.get_queries.return_value = [
        Query(study_key="STUDY1", record_id=501, variable="V_SYNC_DICT", status="OPEN"),
        Query(study_key="STUDY1", record_id=999, variable="V_SYNC_REC", status="OPEN"),
        Query(
            study_key="STUDY1",
            subject_key="UAT-SUBJ-2",
            record_id=503,
            variable="V_ASYNC_JOB",
            status="OPEN",
        ),
    ]

    report = UATExecutionEngine(sdk, dd, poll_interval=0.01).run_verification("STUDY1")
    # Missing subjectKey on one accepted soft rule -> unscoped single get_queries("STUDY1") call
    sdk.get_queries.assert_called_once_with("STUDY1")
    by_name = {r["rule_name"]: r for r in report.results}
    assert by_name["SyncDictSoftPass"]["status"] == EditCheckResultStatus.PASS
    assert by_name["SyncModelSoftFail"]["status"] == EditCheckResultStatus.FAIL
    assert by_name["AsyncJobSoftPass"]["status"] == EditCheckResultStatus.PASS
    assert by_name["HardAcceptedFail"]["status"] == EditCheckResultStatus.FAIL
    assert by_name["HardAcceptedFail"]["actual_response"] == "Accepted"

    # When zero soft rules are accepted (all soft rules rejected), get_queries is never called
    sdk_no_soft = Mock()
    sdk_no_soft.create_record.side_effect = ValidationError("Rejected")
    sdk_no_soft.get_queries.return_value = []
    report_no_soft = UATExecutionEngine(sdk_no_soft, dd).run_verification("STUDY1")
    assert sdk_no_soft.get_queries.call_count == 0
    assert report_no_soft.failed_rules == 3
    assert report_no_soft.passed_rules == 1


def test_uat_engine_wildcard_queries_and_no_cross_query_false_positives():
    """_OpenQueryIndex matches wildcard Queries missing recordId/variable/subjectKey without cross-matching separate Queries."""
    from imednet.models.queries import Query
    from imednet.spi.models import Job, JobStatus

    rules = [
        {
            "Name": "WildcardRecordId",
            "Status": "Active",
            "Form": "F1",
            "Variable": "VAR_NO_REC",
            "Operator": "<=",
            "Value": "5",
            "Severity": "warning",
        },
        {
            "Name": "WildcardVariable",
            "Status": "Active",
            "Form": "F1",
            "Variable": "VAR_ANY",
            "Operator": "<=",
            "Value": "5",
            "Severity": "warning",
        },
        {
            "Name": "WildcardSubjectKey",
            "Status": "Active",
            "Form": "F1",
            "Variable": "VAR_NO_SUBJ",
            "Operator": "<=",
            "Value": "5",
            "Severity": "warning",
        },
        {
            "Name": "CrossQueryMustNotMatch",
            "Status": "Active",
            "Form": "F1",
            "Variable": "VAR_TARGET",
            "Operator": "<=",
            "Value": "5",
            "Severity": "warning",
        },
    ]
    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    sdk = Mock()
    sdk.poll_jobs = None
    sdk.create_record.side_effect = [
        Job(batch_id="b-1", state="PENDING"),
        Job(batch_id="b-2", state="PENDING"),
        Job(batch_id="b-3", state="PENDING"),
        Job(batch_id="b-4", state="PENDING"),
    ]
    rec_map = {"b-1": 701, "b-2": 702, "b-3": 703, "b-4": 704}
    sdk.get_job.side_effect = lambda _s, bid: JobStatus(
        batch_id=bid,
        state="COMPLETED",
        failed_records=0,
        results=[{"recordId": rec_map[bid], "status": "SUCCESS"}],
    )
    sdk.get_queries.return_value = [
        # Missing recordId (wildcard recordId)
        Query(
            study_key="STUDY1",
            subject_key="UAT-SUBJ-0",
            record_id=None,
            variable="VAR_NO_REC",
            status="OPEN",
        ),
        # Missing variable (wildcard variable)
        Query(
            study_key="STUDY1",
            subject_key="UAT-SUBJ-1",
            record_id=702,
            variable=None,
            status="OPEN",
        ),
        # Missing subjectKey (wildcard subjectKey)
        Query(
            study_key="STUDY1",
            subject_key=None,
            record_id=703,
            variable="VAR_NO_SUBJ",
            status="OPEN",
        ),
        # Two partial matches for CrossQueryMustNotMatch (record 704 has wrong var; right var has wrong record 999)
        Query(
            study_key="STUDY1",
            subject_key="UAT-SUBJ-3",
            record_id=704,
            variable="OTHER_VAR",
            status="OPEN",
        ),
        Query(
            study_key="STUDY1",
            subject_key="UAT-SUBJ-3",
            record_id=999,
            variable="VAR_TARGET",
            status="OPEN",
        ),
    ]

    engine = UATExecutionEngine(sdk, dd, poll_interval=0.01)
    report = engine.run_verification("STUDY1")

    assert sdk.get_queries.call_count == 1
    by_name = {r["rule_name"]: r["status"] for r in report.results}
    assert by_name == {
        "WildcardRecordId": EditCheckResultStatus.PASS,
        "WildcardVariable": EditCheckResultStatus.PASS,
        "WildcardSubjectKey": EditCheckResultStatus.PASS,
        "CrossQueryMustNotMatch": EditCheckResultStatus.FAIL,
    }


def test_uat_engine_records_get_queries_errors_per_accepted_soft_rule():
    """UATExecutionEngine records get_queries errors per accepted soft rule without affecting hard rules."""
    from imednet.spi.errors import DiagnosticContext, ServerError
    from imednet.spi.models import Job, JobStatus

    rules = [
        {
            "Name": "SoftErr1",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V_SOFT_1",
            "Operator": "<=",
            "Value": "10",
            "Severity": "warning",
        },
        {
            "Name": "SoftErr2",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V_SOFT_2",
            "Operator": "<=",
            "Value": "10",
            "Severity": "warning",
        },
        {
            "Name": "HardPass",
            "Status": "Active",
            "Form": "F1",
            "Variable": "V_HARD",
            "Operator": ">=",
            "Value": "18",
        },
    ]
    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    diag = DiagnosticContext(
        method="GET",
        url="https://api.test/api/v1/edc/studies/STUDY1/queries",
        attempt_count=2,
    )

    def failing_paginated_queries(_study, **_kwargs):
        yield {"recordId": 801, "variable": "V_SOFT_1", "status": "OPEN"}
        raise ServerError("Queries page 2 failed", status_code=503, diagnostic_context=diag)

    sdk = Mock()
    sdk.poll_jobs = None
    sdk.create_record.side_effect = [
        {"recordId": 801},
        Job(batch_id="b-soft-2", state="PENDING"),
        ValidationError("Hard rule rejected"),
    ]
    sdk.get_job.return_value = JobStatus(
        batch_id="b-soft-2",
        state="COMPLETED",
        failed_records=0,
        results=[{"recordId": 802, "status": "SUCCESS"}],
    )
    sdk.get_queries.side_effect = failing_paginated_queries

    report = UATExecutionEngine(sdk, dd, poll_interval=0.01).run_verification("STUDY1")
    assert sdk.get_queries.call_count == 1
    by_name = {r["rule_name"]: r for r in report.results}
    for soft_name in ("SoftErr1", "SoftErr2"):
        assert by_name[soft_name]["status"] == EditCheckResultStatus.ERROR
        assert "HTTP 503" in by_name[soft_name]["actual_response"]
        assert by_name[soft_name]["diagnostic_context"] == diag
    assert by_name["HardPass"]["status"] == EditCheckResultStatus.PASS
    assert report.passed_rules == 1
    assert report.failed_rules == 2

    # Verify TypeError fallback when custom get_queries accepts only positional study_key
    sdk_pos = Mock()
    sdk_pos.create_record.return_value = {"recordId": "801"}
    sdk_pos.get_queries.side_effect = lambda study: [
        {"recordId": "801", "variable": "V_SOFT_1", "subjectKey": "UAT-SUBJ-0", "status": "OPEN"}
    ]
    single_soft_dd = DataDictionary(business_logic=[rules[0]], choices=[], forms=[], questions=[])
    pos_engine = UATExecutionEngine(sdk_pos, single_soft_dd)
    pos_report = pos_engine.run_verification("STUDY1")
    assert pos_report.passed_rules == 1
    direct_res = {"expected_response": "Query Opened"}
    pos_engine._record_acceptance(  # noqa: SLF001
        "STUDY1",
        direct_res,
        pos_report,
        rule=pos_engine._rules[0],  # noqa: SLF001
        payload={"subjectKey": "UAT-SUBJ-0"},
        job_status=type("SyncRec", (), {"recordId": "801"})(),
    )
    assert direct_res["status"] == EditCheckResultStatus.PASS
