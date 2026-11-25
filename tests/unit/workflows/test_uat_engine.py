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

    baseline_query_items = [
        {
            "annotationId": 9999,
            "studyKey": "STUDY1",
            "subjectKey": "SUBJ-SHARED-1",
            "recordId": 1001,
            "variable": "V_1",
            "status": "OPEN",
        }
    ]
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
    # Pre-existing open query on V_1 (annotationId=9999) remains open post-submission and must be excluded
    all_query_items[1] = dict(baseline_query_items[0])
    # Place newly opened matching queries on page 0 and page 2 (index > 1000) to exercise multi-page indexing
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
        with lock:
            has_submitted = bool(seen_traces)
        active_items = all_query_items if has_submitted else baseline_query_items
        start = page * size
        chunk = active_items[start : start + size]
        total_pages = max(1, (len(active_items) + size - 1) // size)
        return httpx.Response(
            200,
            json={
                "data": chunk,
                "pagination": {
                    "currentPage": page,
                    "size": size,
                    "totalPages": total_pages,
                    "totalElements": len(active_items),
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
    # 1 baseline GET /queries snapshot + 3 post-submission paginated GET /queries requests (1,050 items at size=500)
    assert len(query_requests) == 4
    assert query_requests[0].url.params["page"] == "0"
    assert query_requests[0].url.params["size"] == "500"
    assert "subjectKey==SUBJ-SHARED-1" in query_requests[0].url.params["filter"]
    for page_idx, req in enumerate(query_requests[1:]):
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
    """_OpenQueryIndex rejects None-wildcard Queries on constrained rules while matching omitted caller dimensions."""
    from imednet.models.queries import Query
    from imednet.spi.models import Job, JobStatus
    from imednet_workflows.uat.engine import _OpenQueryIndex

    rules = [
        {
            "Name": "MissingQueryRecordIdMustFail",
            "Status": "Active",
            "Form": "F1",
            "Variable": "VAR_NO_REC",
            "Operator": "<=",
            "Value": "5",
            "Severity": "warning",
        },
        {
            "Name": "MissingQueryVariableMustFail",
            "Status": "Active",
            "Form": "F1",
            "Variable": "VAR_ANY",
            "Operator": "<=",
            "Value": "5",
            "Severity": "warning",
        },
        {
            "Name": "MissingQuerySubjectKeyMustFail",
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
        {
            "Name": "UnconstrainedCallerRecordIdPasses",
            "Status": "Active",
            "Form": "F1",
            "Variable": "VAR_SYNC_NOREC",
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
        {},  # Synchronous response without recordId -> caller omits expected_record_ids
    ]
    rec_map = {"b-1": 701, "b-2": 702, "b-3": 703, "b-4": 704}
    sdk.get_job.side_effect = lambda _s, bid: JobStatus(
        batch_id=bid,
        state="COMPLETED",
        failed_records=0,
        results=[{"recordId": rec_map[bid], "status": "SUCCESS"}],
    )
    sdk.get_queries.return_value = [
        Query(
            study_key="STUDY1",
            subject_key="UAT-SUBJ-0",
            record_id=None,
            variable="VAR_NO_REC",
            status="OPEN",
        ),
        Query(
            study_key="STUDY1",
            subject_key="UAT-SUBJ-1",
            record_id=702,
            variable=None,
            status="OPEN",
        ),
        Query(
            study_key="STUDY1",
            subject_key=None,
            record_id=703,
            variable="VAR_NO_SUBJ",
            status="OPEN",
        ),
        Query(
            study_key="STUDY1",
            subject_key=None,
            record_id=None,
            variable=None,
            status="OPEN",
        ),
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
        Query(
            study_key="STUDY1",
            subject_key="UAT-SUBJ-4",
            record_id=705,
            variable="VAR_SYNC_NOREC",
            status="OPEN",
        ),
    ]

    engine = UATExecutionEngine(sdk, dd, poll_interval=0.01)
    report = engine.run_verification("STUDY1")

    assert sdk.get_queries.call_count == 1
    by_name = {r["rule_name"]: r["status"] for r in report.results}
    assert by_name == {
        "MissingQueryRecordIdMustFail": EditCheckResultStatus.FAIL,
        "MissingQueryVariableMustFail": EditCheckResultStatus.FAIL,
        "MissingQuerySubjectKeyMustFail": EditCheckResultStatus.FAIL,
        "CrossQueryMustNotMatch": EditCheckResultStatus.FAIL,
        "UnconstrainedCallerRecordIdPasses": EditCheckResultStatus.PASS,
    }

    # Direct _OpenQueryIndex checks: omitted rule_vars or subject_key probe _UNCONSTRAINED only
    idx = _OpenQueryIndex.from_queries(
        [
            Query(
                study_key="STUDY1", subject_key="SUB1", record_id=888, variable="V_X", status="OPEN"
            )
        ]
    )
    assert idx.matches(expected_record_ids={888}, rule_vars=set(), subject_key="SUB1") is True
    assert idx.matches(expected_record_ids={888}, rule_vars={"V_X"}, subject_key="") is True
    assert idx.matches(expected_record_ids={888}, rule_vars={"V_Y"}, subject_key="SUB1") is False


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


def test_uat_engine_simulation_transport_soft_rules_exclude_baseline_unattributed_and_dep_vars():
    """Soft rules against SimulationTransport fail with Accepted (No Open Query) despite baseline, wildcard, and dep queries."""
    import httpx

    from imednet import ImednetSDK
    from imednet.simulation import SimulationState, create_simulation_transport

    state = SimulationState()
    # Tag SUB1 as UAT so UATExecutionEngine reuses SUB1 (which already has open queries 701 & 702)
    state.subjects[0]["keywords"] = [{"keywordName": "UAT", "keywordKey": "UAT"}]
    state.queries.extend(
        [
            {
                "annotationId": 703,
                "studyKey": "STUDY",
                "subjectKey": "SUB1",
                "recordId": None,
                "variable": None,
                "status": "OPEN",
            },
            {
                "annotationId": 704,
                "studyKey": "STUDY",
                "subjectKey": None,
                "recordId": None,
                "variable": None,
                "status": "OPEN",
            },
        ]
    )

    sim_transport = create_simulation_transport(state)
    post_injected = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_injected
        response = sim_transport.handle_request(request)
        if request.method == "POST" and request.url.path.endswith("/records") and not post_injected:
            post_injected = True
            # Inject new post-submission open queries on (None, None, SUB1), (None, None, None),
            # and on the dependency variable V_SEX for the newly created recordId 1001.
            state.queries.extend(
                [
                    {
                        "annotationId": 901,
                        "studyKey": "STUDY",
                        "subjectKey": "SUB1",
                        "recordId": None,
                        "variable": None,
                        "status": "OPEN",
                    },
                    {
                        "annotationId": 902,
                        "studyKey": "STUDY",
                        "subjectKey": None,
                        "recordId": None,
                        "variable": None,
                        "status": "OPEN",
                    },
                    {
                        "annotationId": 903,
                        "studyKey": "STUDY",
                        "subjectKey": "SUB1",
                        "recordId": 1001,
                        "variable": "V_SEX",
                        "status": "OPEN",
                    },
                ]
            )
        return response

    rules = [
        {
            "Name": "SoftWeightWithDep",
            "Status": "Active",
            "Form": "F_VS",
            "Variable": "V_WEIGHT",
            "Operator": ">=",
            "Value": "50",
            "Dependency Variable": "V_SEX",
            "Dependency Value": "F",
            "Check Type": "Query",
        },
        {
            "Name": "SoftBpSubjectWildcard",
            "Status": "Active",
            "Form": "F_VS",
            "Variable": "V_BP_SYS",
            "Operator": "<=",
            "Value": "140",
            "Check Type": "Query",
        },
    ]
    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    with ImednetSDK(
        transport=httpx.MockTransport(handler),
        simulation_mode=True,
    ) as sdk:
        report = UATExecutionEngine(sdk, dd, poll_interval=0.01).run_verification("STUDY")

    assert report.passed_rules == 0
    assert report.failed_rules == 2
    for res in report.results:
        assert res["status"] == EditCheckResultStatus.FAIL
        assert res["actual_response"] == "Accepted (No Open Query)"


def test_uat_engine_legacy_query_status_delegation_and_annotation_id_edges():
    """_is_query_open delegates legacy comment threads to calculate_legacy_status."""
    from imednet.models.queries import Query, QueryComment
    from imednet.spi.models import QueryStatus, calculate_legacy_status
    from imednet_workflows.uat.engine import _extract_query_annotation_id

    comment_cases: list[tuple[list[dict[str, object]], bool]] = [
        # Monitor Answered -> ANSWERED (non-open)
        (
            [
                {"sequence": 1, "annotationStatus": "Open", "closed": False},
                {"sequence": 2, "annotationStatus": "Monitor Answered", "closed": False},
            ],
            False,
        ),
        # Cancelled -> CANCELLED (non-open)
        (
            [
                {"sequence": 1, "annotationStatus": "Open", "closed": False},
                {"sequence": 2, "annotationStatus": "Cancelled", "closed": None},
            ],
            False,
        ),
        (
            [
                {"sequence": 1, "annotationStatus": "Open", "closed": False},
                {"sequence": 2, "annotationStatus": "Cancelled", "closed": True},
            ],
            False,
        ),
        # Out-of-order list where sequence=2 Closed precedes sequence=1 Open -> CLOSED (non-open)
        (
            [
                {"sequence": 2, "annotationStatus": "Closed", "closed": True},
                {"sequence": 1, "annotationStatus": "Open", "closed": False},
            ],
            False,
        ),
        # Latest sequence=3 reopened -> OPEN
        (
            [
                {"sequence": 2, "annotationStatus": "Closed", "closed": True},
                {"sequence": 1, "annotationStatus": "Open", "closed": False},
                {"sequence": 3, "annotationStatus": "Open", "closed": False},
            ],
            True,
        ),
    ]
    for raw_comments, expected_bool in comment_cases:
        dict_query = {"studyKey": "STUDY", "queryComments": raw_comments}
        model_comments = [QueryComment.model_validate(c) for c in raw_comments]
        model_query = Query.model_construct(
            study_key="STUDY",
            status=None,
            query_comments=model_comments,
        )
        expected_open = calculate_legacy_status(model_comments) == QueryStatus.OPEN
        assert expected_open is expected_bool
        assert UATExecutionEngine._is_query_open(dict_query) is expected_bool  # noqa: SLF001
        assert UATExecutionEngine._is_query_open(model_query) is expected_bool  # noqa: SLF001
    assert UATExecutionEngine._is_query_open({"studyKey": "STUDY"}) is False  # noqa: SLF001

    assert _extract_query_annotation_id({"annotationId": 701}) == 701
    assert _extract_query_annotation_id({"annotation_id": "42"}) == 42
    assert _extract_query_annotation_id({"annotationId": True}) is None
    assert _extract_query_annotation_id({"annotationId": 0}) is None
    assert _extract_query_annotation_id({"annotationId": "not-an-int"}) is None


def test_uat_engine_tabular_rule_normalization_typed_scalars_and_companion_fields():
    """generate_negative_test_case normalizes tabular rules and populates typed companion Required='Yes' fields."""
    from imednet.datadict import Choice
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVariable

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="F_TAB",
                variables=[
                    DDVariable(variableName="AGE_GTE", variableType="Integer", required="No"),
                    DDVariable(variableName="AGE_GT", variableType="Integer", required="No"),
                    DDVariable(variableName="AGE_LTE", variableType="Integer", required="No"),
                    DDVariable(variableName="AGE_LT", variableType="Integer", required="No"),
                    DDVariable(variableName="STATUS_EQ", variableType="Text", required="No"),
                    DDVariable(variableName="STATUS_NE", variableType="Text", required="No"),
                    DDVariable(variableName="REQ_TARGET", variableType="Text", required="Yes"),
                    DDVariable(variableName="DEP_NO_VAL", variableType="Text", required="No"),
                    DDVariable(variableName="DEP_FLAG", variableType="Yes/No", required="No"),
                    DDVariable(variableName="REQ_INT", variableType="Integer", required="Yes"),
                    DDVariable(variableName="REQ_FLT", variableType="Float", required="Yes"),
                    DDVariable(variableName="REQ_BOOL", variableType="Boolean", required="Yes"),
                    DDVariable(variableName="REQ_YN", variableType="Yes/No", required="Yes"),
                    DDVariable(variableName="REQ_DATE", variableType="Date", required="Yes"),
                    DDVariable(variableName="REQ_DT", variableType="DateTime", required="Yes"),
                    DDVariable(variableName="REQ_TIME", variableType="Time", required="Yes"),
                    DDVariable(variableName="REQ_CALC", variableType="Calculation", required="Yes"),
                    DDVariable(
                        variableName="REQ_CHOICE",
                        variableType="Dropdown",
                        required="Yes",
                        choices=[Choice(choiceText="Option A", choiceValue="OPT_A")],
                    ),
                ],
            )
        ],
        business_logic=[
            {
                "Name": "IntGte90",
                "Status": "Active",
                "Form": "F_TAB",
                "Variable": "AGE_GTE",
                "Operator": ">=",
                "Value": "90",
            },
            {
                "Name": "IntGt40",
                "Status": "Active",
                "Form": "F_TAB",
                "Variable": "AGE_GT",
                "Operator": ">",
                "Value": "40",
            },
            {
                "Name": "IntLte10",
                "Status": "Active",
                "Form": "F_TAB",
                "Variable": "AGE_LTE",
                "Operator": "<=",
                "Value": "10",
            },
            {
                "Name": "IntLt10",
                "Status": "Active",
                "Form": "F_TAB",
                "Variable": "AGE_LT",
                "Operator": "<",
                "Value": "10",
            },
            {
                "Name": "EqRule",
                "Status": "Active",
                "Form": "F_TAB",
                "Variable": "STATUS_EQ",
                "Operator": "==",
                "Value": "ACTIVE",
            },
            {
                "Name": "NeqRule",
                "Status": "Active",
                "Form": "F_TAB",
                "Variable": "STATUS_NE",
                "Operator": "!=",
                "Value": "VOID",
            },
            {
                "Name": "RequiredRule",
                "Status": "Active",
                "Form": "F_TAB",
                "Variable": "REQ_TARGET",
                "Operator": "Required",
                "Value": "",
            },
            {
                "Name": "DepMissingValRule",
                "Status": "Active",
                "Form": "F_TAB",
                "Variable": "DEP_NO_VAL",
                "Operator": ">=",
                "Value": None,
                "Dependency Variable": "DEP_FLAG",
                "Dependency Value": "Yes",
            },
        ],
    )

    engine = UATExecutionEngine(Mock(), dd)
    rules_by_name = {r["rule_name"]: r for r in engine._rules}  # noqa: SLF001

    gte_data = engine.generate_negative_test_case(rules_by_name["IntGte90"])
    assert gte_data["AGE_GTE"] == 89
    assert isinstance(gte_data["AGE_GTE"], int) and not isinstance(gte_data["AGE_GTE"], bool)
    assert isinstance(gte_data["REQ_INT"], int)
    assert isinstance(gte_data["REQ_FLT"], float)
    assert gte_data["REQ_BOOL"] is True
    assert gte_data["REQ_YN"] == "Yes"
    assert gte_data["REQ_DATE"] == "2026-01-01"
    assert "T" in gte_data["REQ_DT"]
    assert gte_data["REQ_TIME"] == "12:00:00"
    assert isinstance(gte_data["REQ_CALC"], int)
    assert gte_data["REQ_CHOICE"] == "OPT_A"

    gt_data = engine.generate_negative_test_case(rules_by_name["IntGt40"])
    assert gt_data["AGE_GT"] == 39
    assert isinstance(gt_data["AGE_GT"], int)

    lte_data = engine.generate_negative_test_case(rules_by_name["IntLte10"])
    assert lte_data["AGE_LTE"] == 11

    lt_data = engine.generate_negative_test_case(rules_by_name["IntLt10"])
    assert lt_data["AGE_LT"] >= 10
    assert isinstance(lt_data["AGE_LT"], int)

    eq_data = engine.generate_negative_test_case(rules_by_name["EqRule"])
    assert eq_data["STATUS_EQ"] != "ACTIVE"

    neq_data = engine.generate_negative_test_case(rules_by_name["NeqRule"])
    assert neq_data["STATUS_NE"] == "VOID"

    req_data = engine.generate_negative_test_case(rules_by_name["RequiredRule"])
    assert req_data["REQ_TARGET"] == ""
    assert req_data["REQ_CHOICE"] == "OPT_A"

    dep_data = engine.generate_negative_test_case(rules_by_name["DepMissingValRule"])
    assert dep_data["DEP_FLAG"] == "Yes"
    assert dep_data["DEP_NO_VAL"] == "Invalid_Value"


def test_uat_engine_enrollment_form_rules_and_site_scoped_subject_isolation_with_simulation():
    """Enrollment rules omit subjectKey/skip prerequisite subject; non-enrollment rules isolate UAT subjects by Site."""
    import json

    import httpx

    from imednet import ImednetSDK
    from imednet.simulation import SimulationState, create_simulation_transport
    from imednet.spi.errors import ServerError

    state = SimulationState()
    state.subjects[0]["keywords"] = [{"keywordName": "UAT", "keywordKey": "UAT"}]
    state.sites.append(
        {
            "siteId": 104,
            "studyKey": "STUDY",
            "siteName": "South Site",
            "siteEnrollmentStatus": "Enrollment Open",
        }
    )

    sim_transport = create_simulation_transport(state)
    posted_batches: list[list[dict]] = []

    def enrollment_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/records"):
            posted_batches.append(json.loads(request.content.decode("utf-8")))
        resp = sim_transport.handle_request(request)
        if request.method == "POST" and request.url.path.endswith("/records"):
            # Open a query on the newly enrolled subject SUBJ-1001 (recordId=1001)
            state.queries.append(
                {
                    "annotationId": 8801,
                    "studyKey": "STUDY",
                    "subjectKey": "SUBJ-1001",
                    "recordId": 1001,
                    "variable": "V_AGE",
                    "status": "OPEN",
                }
            )
        return resp

    # Part 1: Enrollment-only rule omits subjectKey, skips prerequisite subject, and matches job subjectKey
    enrollment_dd = DataDictionary(
        business_logic=[
            {
                "Name": "EnrollmentAgeSoft",
                "Status": "Active",
                "Form": "F_DM",
                "Variable": "V_AGE",
                "Operator": ">=",
                "Value": "21",
                "Check Type": "Query",
            }
        ],
        choices=[],
        forms=[],
        questions=[],
    )
    with ImednetSDK(
        transport=httpx.MockTransport(enrollment_handler),
        simulation_mode=True,
    ) as sdk:
        enr_report = UATExecutionEngine(sdk, enrollment_dd, poll_interval=0.01).run_verification(
            "STUDY",
            site_name="South Site",
        )

    assert len(posted_batches) == 1
    assert "subjectKey" not in posted_batches[0][0]
    assert "intervalName" not in posted_batches[0][0]
    assert posted_batches[0][0]["siteName"] == "South Site"
    assert enr_report.passed_rules == 1
    assert enr_report.results[0]["status"] == EditCheckResultStatus.PASS

    # Part 2: Fresh state at South Site with SUB1 tagged UAT at SITE -> never reuses SUB1 from SITE
    state2 = SimulationState()
    state2.subjects[0]["keywords"] = [{"keywordName": "UAT", "keywordKey": "UAT"}]
    state2.sites.append(
        {
            "siteId": 104,
            "studyKey": "STUDY",
            "siteName": "South Site",
            "siteEnrollmentStatus": "Enrollment Open",
        }
    )
    sim_transport2 = create_simulation_transport(state2)
    south_posts: list[list[dict]] = []

    def south_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/records"):
            south_posts.append(json.loads(request.content.decode("utf-8")))
        return sim_transport2.handle_request(request)

    non_enr_dd = DataDictionary(
        business_logic=[
            {
                "Name": "SouthSiteWeightSoft",
                "Status": "Active",
                "Form": "F_VS",
                "Variable": "V_WEIGHT",
                "Operator": ">=",
                "Value": "50",
                "Check Type": "Query",
            }
        ],
        choices=[],
        forms=[],
        questions=[],
    )
    with ImednetSDK(
        transport=httpx.MockTransport(south_handler),
        simulation_mode=True,
    ) as sdk2:
        south_report = UATExecutionEngine(sdk2, non_enr_dd, poll_interval=0.01).run_verification(
            "STUDY",
            site_name="South Site",
        )

    assert len(south_posts) == 2  # 1 prerequisite enrollment at South Site + 1 F_VS submission
    assert south_posts[0][0]["formKey"] == "F_DM"
    assert south_posts[0][0]["siteName"] == "South Site"
    assert "subjectKey" not in south_posts[0][0]
    assert south_posts[1][0]["formKey"] == "F_VS"
    assert south_posts[1][0]["subjectKey"] == "SUBJ-1001"
    assert south_posts[1][0]["subjectKey"] != "SUB1"
    assert south_report.failed_rules == 1

    # Part 3: Baseline query snapshot error marks accepted soft rule as ERROR
    err_sdk = Mock()
    err_sdk.poll_jobs = None
    err_sdk.get_subjects.return_value = [
        {"subjectKey": "SUBJ-S", "siteName": "South Site", "keywords": ["UAT"]}
    ]
    err_sdk.create_record.return_value = {"recordId": 55}
    err_sdk.get_queries.side_effect = ServerError("Baseline query HTTP 503", status_code=503)
    err_report = UATExecutionEngine(err_sdk, non_enr_dd).run_verification(
        "STUDY",
        site_name="South Site",
    )
    assert err_report.results[0]["status"] == EditCheckResultStatus.ERROR
    assert "503" in err_report.results[0]["actual_response"]


def test_uat_engine_prerequisite_snapshot_types_job_extractors_and_batch_states():
    """Cover snapshot prerequisite variable types, job extractors, and polled batch failure states."""
    from types import SimpleNamespace

    from imednet.datadict import DataDictionary
    from imednet.datadict import Form as DDForm
    from imednet.spi.models import Form as CoreForm
    from imednet.spi.models import Site as CoreSite
    from imednet.spi.models import Variable as CoreVariable
    from imednet.spi.utils import JobPollSummary
    from imednet_workflows.uat.inspector import StudySnapshot

    snap = StudySnapshot(
        study_key="STUDY",
        forms=[
            CoreForm(
                form_id=1,
                form_key="F_ENR",
                form_name="Enrollment",
                form_type="Subject",
                subject_record_report=True,
            )
        ],
        variables=[
            CoreVariable(
                variable_id=1,
                variable_name="S_INT",
                form_key="F_ENR",
                variable_type="Integer",
                required=True,
            ),
            CoreVariable(
                variable_id=2,
                variable_name="S_FLT",
                form_key="F_ENR",
                variable_type="Float",
                required=True,
            ),
            CoreVariable(
                variable_id=3,
                variable_name="S_BOOL",
                form_key="F_ENR",
                variable_type="Boolean",
                required=True,
            ),
            CoreVariable(
                variable_id=4,
                variable_name="S_DATE",
                form_key="F_ENR",
                variable_type="Date",
                required=True,
            ),
            CoreVariable(
                variable_id=5,
                variable_name="S_DT",
                form_key="F_ENR",
                variable_type="DateTime",
                required=True,
            ),
            CoreVariable(
                variable_id=6,
                variable_name="S_TIME",
                form_key="F_ENR",
                variable_type="Time",
                required=True,
            ),
            CoreVariable(
                variable_id=7,
                variable_name="S_YN",
                form_key="F_ENR",
                variable_type="Yes/No",
                required=True,
            ),
            CoreVariable(
                variable_id=8,
                variable_name="S_TXT",
                form_key="F_ENR",
                variable_type="Text",
                required=True,
            ),
            CoreVariable(
                variable_id=9,
                variable_name="S_DEL",
                form_key="F_ENR",
                variable_type="Text",
                required=True,
                deleted=True,
            ),
            CoreVariable(
                variable_id=10,
                variable_name="S_OPT",
                form_key="F_ENR",
                variable_type="Text",
                required=False,
            ),
        ],
        intervals=[],
        sites=[
            CoreSite(
                site_id=1,
                study_key="STUDY",
                site_name="SITE",
                site_enrollment_status="Enrollment Open",
            )
        ],
    )

    raw_container = SimpleNamespace(
        business_logic=[
            {
                "Name": "RawDictDepRule",
                "Status": "Active",
                "Form": "F_STD",
                "Variable": "V_OUT",
                "Operator": "==",
                "Value": "OK",
                "Dependency Variable": "V_DEP",
                "Dependency Value": "1",
            }
        ]
    )
    engine = UATExecutionEngine(Mock(), raw_container)  # type: ignore[arg-type]
    neg = engine.generate_negative_test_case(engine._rules[0])  # noqa: SLF001
    assert neg["V_DEP"] == "1"
    assert neg["V_OUT"] == "NOT_OK"

    reg_data = engine._build_prerequisite_registration_data("F_ENR", snap)  # noqa: SLF001
    assert reg_data == {
        "S_INT": 18,
        "S_FLT": 50.0,
        "S_BOOL": True,
        "S_DATE": "2026-01-01",
        "S_DT": "2026-01-01T12:00:00+00:00",
        "S_TIME": "12:00:00",
        "S_YN": "Yes",
        "S_TXT": "UAT",
    }

    # Job recordId & subjectKey extraction across nested mapping/object shapes
    assert engine._extract_job_record_ids(None) == set()  # noqa: SLF001
    assert engine._extract_job_record_ids({"results": {"records": [{"recordId": "44"}]}}) == {44}  # noqa: SLF001
    assert engine._extract_job_record_ids({"record_id": 55}) == {55}  # noqa: SLF001
    assert engine._extract_job_subject_key(None) is None  # noqa: SLF001
    assert (
        engine._extract_job_subject_key({"results": {"records": [{"subjectKey": "SUBJ-77"}]}})  # noqa: SLF001
        == "SUBJ-77"
    )
    assert engine._extract_job_subject_key({"subject_key": "SUBJ-88"}) == "SUBJ-88"  # noqa: SLF001
    assert (
        engine._extract_job_subject_key(SimpleNamespace(results=None, subject_key="SUBJ-99"))  # noqa: SLF001
        == "SUBJ-99"
    )
    assert engine._extract_rule_variables({"condition_variables": [" V_A ", ""]}) == {"V_A"}  # noqa: SLF001

    # Polled batch classification for TIMEOUT, structural error, and missing batch result
    from imednet_workflows.uat.engine import EditCheckVerificationReport

    rep = EditCheckVerificationReport(study_key="STUDY")
    summary = JobPollSummary(
        study_key="STUDY",
        elapsed_total=0.1,
        results={
            "b-timeout": SimpleNamespace(batch_id="b-timeout", state="TIMEOUT"),
            "b-struct": SimpleNamespace(
                batch_id="b-struct",
                state="FAILED",
                result_error="Form not found: F_MISSING",
            ),
        },
        failures={"b-exc": RuntimeError("network drop")},
    )
    for bid, expected_substr in (
        ("b-timeout", "timed out"),
        ("b-struct", "Form not found"),
        ("b-exc", "network drop"),
        ("b-missing", "Missing poll status"),
    ):
        r_entry: dict[str, object] = {"expected_response": "Rejected"}
        engine._classify_polled_batch(r_entry, bid, summary, rep)  # noqa: SLF001
        assert r_entry["status"] == EditCheckResultStatus.ERROR
        assert expected_substr in str(r_entry["actual_response"])

    # Fallback enrollment form resolution from DataDictionary when snapshot has no enrollment form
    dd_enr = DataDictionary(
        forms=[DDForm(formKey="F_DD_ENR", formType="Enrollment")],
    )
    sdk_dd = Mock()
    sdk_dd.poll_jobs = None
    sdk_dd.get_job = Mock()
    sdk_dd.get_subjects.side_effect = [
        [],
        [{"subjectKey": "SUBJ-DD-1", "siteName": "SITE", "keywords": []}],
    ]
    sdk_dd.create_record.return_value = {}
    eng_dd = UATExecutionEngine(sdk_dd, dd_enr)
    resolved_sk = eng_dd._ensure_prerequisite_subject(  # noqa: SLF001
        "STUDY",
        snapshot=None,
        site_name="SITE",
        interval=0.0,
        timeout=1.0,
    )
    assert resolved_sk == "SUBJ-DD-1"
