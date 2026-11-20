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
