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
    # Rule1 was accepted (FAIL) and RuleError raised generic ApiError (ERROR) -> 2 non-passing rules
    assert report.failed_rules == 2
    assert report.skipped_rules == 1
    status_by_rule = {r["rule_name"]: r["status"] for r in report.results}
    assert status_by_rule["Rule1"] == EditCheckResultStatus.FAIL
    assert status_by_rule["RuleError"] == EditCheckResultStatus.ERROR
    assert status_by_rule["RuleSkip"] == EditCheckResultStatus.SKIPPED


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

    from imednet.errors import ApiError, BadRequestError, ServerError
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
            raise BadRequestError("HTTP 400 validation error", status_code=400)
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
    engine = UATExecutionEngine(sdk, dd, poll_interval=0.01, poll_timeout=5.0, batch_size=1)

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
        # System FAILED job without record rejections classifies as ERROR
        "AsyncFailedState": EditCheckResultStatus.ERROR,
        "AsyncFailedRecords": EditCheckResultStatus.PASS,
        "AsyncAccepted": EditCheckResultStatus.FAIL,
        "AsyncTimeout": EditCheckResultStatus.ERROR,
        "SyncClientReject": EditCheckResultStatus.PASS,
        "SyncServerErr": EditCheckResultStatus.ERROR,
        "SyncApi500Err": EditCheckResultStatus.ERROR,
    }
    assert report.total_rules == 7
    assert report.passed_rules == 2
    assert report.failed_rules == 5


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
    sdk.create_record.side_effect = [Job(batch_id=f"batch-{i}", state="PENDING") for i in range(5)]
    sdk.get_job.side_effect = lambda _study, bid: JobStatus(
        batch_id=bid, state="COMPLETED", failed_records=25
    )

    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    engine = UATExecutionEngine(sdk, dd, poll_interval=0.01, poll_timeout=5.0, batch_size=25)

    with patch.object(JobPoller, "poll_many", autospec=True, wraps=JobPoller.poll_many) as spy_poll:
        report = engine.run_verification("STUDY1")

    # 135 active rules capped at 100 and batched via BatchCoordinator into 4 chunks of 25
    assert sdk.create_record.call_count == 4
    assert [len(c.args[1]) for c in sdk.create_record.call_args_list] == [25, 25, 25, 25]
    spy_poll.assert_called_once()
    assert len(spy_poll.call_args.args[2]) == 4
    assert report.total_rules == 100
    assert report.passed_rules == 100
    assert report.failed_rules == 0


def test_uat_engine_enrolls_or_reuses_subjects_attaches_intervals_and_fetches_result_url() -> None:
    """UATExecutionEngine honors site_name, enrolls/reuses UAT subjects, uses RegisterSubjectRequest on Enrollment forms, attaches intervalName on scheduled forms, and fetches resultUrl via fallback JobPoller."""
    import json

    import httpx

    from imednet import ImednetSDK

    posted_batches: list[list[dict]] = []
    enrolled_subjects: list[dict] = []
    result_url_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/sites"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY1",
                            "siteId": 1,
                            "siteName": "SiteA",
                            "siteEnrollmentStatus": "Active",
                        },
                        {
                            "studyKey": "STUDY1",
                            "siteId": 2,
                            "siteName": "SiteB",
                            "siteEnrollmentStatus": "Enrollment Open",
                        },
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/forms"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY1",
                            "formId": 10,
                            "formKey": "F_ENROLL",
                            "formName": "Enrollment",
                            "formType": "Enrollment",
                        },
                        {
                            "studyKey": "STUDY1",
                            "formId": 20,
                            "formKey": "F_SCHED",
                            "formName": "Scheduled Visit",
                            "formType": "DataEntry",
                            "unscheduledVisit": False,
                        },
                        {
                            "studyKey": "STUDY1",
                            "formId": 30,
                            "formKey": "F_UNSCHED",
                            "formName": "Adverse Event",
                            "formType": "DataEntry",
                            "unscheduledVisit": True,
                        },
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/intervals"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY1",
                            "intervalId": 100,
                            "intervalName": "Baseline",
                            "forms": [
                                {"formId": 20, "formKey": "F_SCHED", "formName": "Scheduled Visit"}
                            ],
                        }
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/subjects"):
            return httpx.Response(200, json={"data": list(enrolled_subjects)})
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            posted_batches.append(body)
            bid = f"B-{len(posted_batches)}"
            # Enrollment pre-step populates study subject roster with valid AGE >= 18
            is_valid_enroll = (
                body
                and all(p.get("formKey") == "F_ENROLL" for p in body)
                and float(body[0].get("data", {}).get("AGE", 0)) >= 18
            )
            if is_valid_enroll:
                for n, item in enumerate(body, start=1):
                    enrolled_subjects.append(
                        {
                            "studyKey": "STUDY1",
                            "subjectId": 500 + n,
                            "subjectKey": f"UAT-SUBJ-50{n}",
                            "siteName": item["siteName"],
                            "deleted": False,
                        }
                    )
            return httpx.Response(
                200, json={"jobId": f"J-{bid}", "batchId": bid, "state": "QUEUED"}
            )
        if request.method == "GET" and "/jobs/B-" in path and not path.endswith("/results"):
            bid = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "jobId": f"J-{bid}",
                    "batchId": bid,
                    "state": "COMPLETED",
                    "resultUrl": f"https://mock.imednet.test/api/v1/edc/studies/STUDY1/jobs/{bid}/results",
                },
            )
        if request.method == "GET" and path.endswith("/results"):
            bid = path.split("/")[-2]
            result_url_calls.append(bid)
            idx = int(bid.split("-")[1]) - 1
            batch = posted_batches[idx]
            if (
                batch
                and all(p.get("formKey") == "F_ENROLL" for p in batch)
                and float(batch[0].get("data", {}).get("AGE", 0)) >= 18
            ):
                return httpx.Response(
                    200,
                    json=[
                        {"subjectKey": f"UAT-SUBJ-50{n}", "status": "SUCCESS"}
                        for n in range(1, len(batch) + 1)
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {"status": "FAILED", "message": "Edit check rule rejected field"} for _ in batch
                ],
            )
        return httpx.Response(404)

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        # Proxy without poll_jobs to exercise fallback JobPoller + resolve_sdk_result_fetcher
        class _FallbackEngineSDK:
            def __init__(self, inner: ImednetSDK) -> None:
                self.jobs = inner.jobs
                self.create_record = inner.create_record
                self.get_job = inner.get_job
                self.get_sites = inner.get_sites
                self.get_forms = inner.get_forms
                self.get_intervals = inner.get_intervals
                self.get_subjects = inner.get_subjects

        dd = DataDictionary(
            business_logic=[
                {
                    "Name": "EnrollRule",
                    "Status": "Active",
                    "Form": "F_ENROLL",
                    "Variable": "AGE",
                    "Operator": ">=",
                    "Value": "18",
                },
                {
                    "Name": "SchedRule1",
                    "Status": "Active",
                    "Form": "F_SCHED",
                    "Variable": "SYSBP",
                    "Operator": "<=",
                    "Value": "140",
                },
                {
                    "Name": "SchedRule2",
                    "Status": "Active",
                    "Form": "F_SCHED",
                    "Variable": "DIABP",
                    "Operator": "<=",
                    "Value": "90",
                },
                {
                    "Name": "UnschedRule",
                    "Status": "Active",
                    "Form": "F_UNSCHED",
                    "Variable": "AETERM",
                    "Operator": "Required",
                    "Value": "Required",
                },
            ],
            choices=[],
            forms=[],
            questions=[],
        )
        engine = UATExecutionEngine(_FallbackEngineSDK(sdk), dd, poll_interval=0.0)
        report = engine.run_verification("STUDY1", site_name="SiteB")

        assert report.total_rules == 4
        assert report.passed_rules == 4
        assert report.failed_rules == 0
        # Pre-enrollment batch + verification batch (2 distinct subjekts needed for two F_SCHED rules)
        # First POST enrolled UAT subjects for SiteB; second POST batched all 4 verification payloads
        enroll_batch = posted_batches[0]
        assert all(p["siteName"] == "SiteB" and "subjectKey" not in p for p in enroll_batch)
        verify_batch = posted_batches[-1]
        assert len(verify_batch) == 4
        # Enrollment rule payload uses siteName without subjectKey (RegisterSubjectRequest)
        enroll_rule_payload = next(p for p in verify_batch if p["formKey"] == "F_ENROLL")
        assert enroll_rule_payload["siteName"] == "SiteB"
        assert "subjectKey" not in enroll_rule_payload
        # Scheduled rule payloads attach intervalName="Baseline" and valid UAT subjectKey
        sched_payloads = [p for p in verify_batch if p["formKey"] == "F_SCHED"]
        assert len(sched_payloads) == 2
        for sp in sched_payloads:
            assert sp["intervalName"] == "Baseline"
            assert sp["subjectKey"].startswith("UAT-SUBJ-")
        # Unscheduled rule payload uses subjectKey without intervalName
        unsched_payload = next(p for p in verify_batch if p["formKey"] == "F_UNSCHED")
        assert unsched_payload["subjectKey"].startswith("UAT-SUBJ-")
        assert "intervalName" not in unsched_payload
        assert len(result_url_calls) >= 2
    finally:
        sdk.close()


def test_uat_engine_strict_error_classifications_unconfigured_mocks_and_phi_redaction() -> None:
    """UATExecutionEngine classifies 401/403/404/429, CANCELLED, system FAILED, unknown-Subject, and unconfigured mock get_job as ERROR, 400/422 rule rejections as PASS, and redacts PHI."""
    from imednet.spi.errors import (
        BadRequestError,
        ForbiddenError,
        NotFoundError,
        RateLimitError,
        UnauthorizedError,
        ValidationError,
    )
    from imednet.spi.models import Job, JobStatus

    rules = [
        {
            "Name": name,
            "Status": "Active",
            "Form": "F1",
            "Variable": f"V_{idx}",
            "Operator": ">=",
            "Value": "18",
        }
        for idx, name in enumerate(
            [
                "Http400Pass",
                "Http422Pass",
                "Http401Err",
                "Http403Err",
                "Http404Err",
                "Http429Err",
                "Http400UnknownSubjErr",
                "JobCancelledErr",
                "JobUnknownSubjErr",
                "JobRuleRejectedPass",
            ]
        )
    ]

    sdk = Mock()

    def create_side_effect(_study: str, payloads: list[dict], **_kwargs):
        data = payloads[0]["data"]
        if "V_0" in data:
            raise BadRequestError("Rule >= 18 violated for patient@example.com", status_code=400)
        if "V_1" in data:
            raise ValidationError("422 edit check violated", status_code=422)
        if "V_2" in data:
            raise UnauthorizedError(
                "401 invalid credentials api_key=SECRET-KEY-999", status_code=401
            )
        if "V_3" in data:
            raise ForbiddenError("403 forbidden Bearer SUPER-SECRET-JWT", status_code=403)
        if "V_4" in data:
            raise NotFoundError("404 study endpoint not found", status_code=404)
        if "V_5" in data:
            raise RateLimitError("429 rate limit exceeded", status_code=429)
        if "V_6" in data:
            raise BadRequestError("Subject not found: SUBJ-999", status_code=400)
        if "V_7" in data:
            return Job(batch_id="b-cancelled", state="PENDING")
        if "V_8" in data:
            return Job(batch_id="b-unknown-subj", state="PENDING")
        return Job(batch_id="b-rule-pass", state="PENDING")

    def get_job_side_effect(_study: str, bid: str):
        if bid == "b-cancelled":
            return JobStatus(batch_id=bid, state="CANCELLED")
        if bid == "b-unknown-subj":
            return JobStatus(
                batch_id=bid,
                state="COMPLETED",
                failed_records=1,
                results=[
                    {
                        "status": "FAILED",
                        "message": "Unknown subject key SUBJ-404 token=RAW-SECRET-TOKEN",
                    }
                ],
            )
        return JobStatus(
            batch_id=bid,
            state="COMPLETED",
            failed_records=1,
            results=[{"status": "FAILED", "message": "Value must be >= 18"}],
        )

    sdk.create_record.side_effect = create_side_effect
    sdk.get_job.side_effect = get_job_side_effect

    dd = DataDictionary(business_logic=rules, choices=[], forms=[], questions=[])
    engine = UATExecutionEngine(sdk, dd, poll_interval=0.0, batch_size=1)
    report = engine.run_verification("STUDY1")

    by_name = {r["rule_name"]: r for r in report.results}
    assert by_name["Http400Pass"]["status"] == EditCheckResultStatus.PASS
    assert by_name["Http422Pass"]["status"] == EditCheckResultStatus.PASS
    assert by_name["JobRuleRejectedPass"]["status"] == EditCheckResultStatus.PASS

    for err_rule in (
        "Http401Err",
        "Http403Err",
        "Http404Err",
        "Http429Err",
        "Http400UnknownSubjErr",
        "JobCancelledErr",
        "JobUnknownSubjErr",
    ):
        assert by_name[err_rule]["status"] == EditCheckResultStatus.ERROR
        # Verify sensitive tokens/credentials are redacted in error text via redact_sensitive_payload
        assert "SECRET-KEY-999" not in by_name[err_rule]["actual_response"]
        assert "SUPER-SECRET-JWT" not in by_name[err_rule]["actual_response"]
        assert "RAW-SECRET-TOKEN" not in by_name[err_rule]["actual_response"]

    # Verify clinical PHI fields in report.results["test_data"] are masked
    phi_dd = DataDictionary(
        business_logic=[
            {
                "Name": "PhiSsnRule",
                "Status": "Active",
                "Form": "F1",
                "Variable": "ssn",
                "Operator": "==",
                "Value": "123-45-6789",
            }
        ],
        choices=[],
        forms=[],
        questions=[],
    )
    phi_sdk = Mock()
    phi_sdk.create_record.side_effect = ValidationError("Rejected SSN", status_code=422)
    phi_report = UATExecutionEngine(phi_sdk, phi_dd).run_verification("STUDY1")
    assert phi_report.results[0]["status"] == EditCheckResultStatus.PASS
    assert phi_report.results[0]["test_data"]["ssn"] == "***MASKED***"

    # Also verify unconfigured mock get_job is detected and classified as ERROR instead of spinning
    unconfigured_sdk = Mock()
    unconfigured_sdk.create_record.return_value = Job(batch_id="b-unconf", state="PENDING")
    single_dd = DataDictionary(business_logic=[rules[0]], choices=[], forms=[], questions=[])
    unconf_report = UATExecutionEngine(
        unconfigured_sdk, single_dd, poll_interval=0.0
    ).run_verification("STUDY1")
    assert unconf_report.results[0]["status"] == EditCheckResultStatus.ERROR
    assert "Unconfigured mock" in unconf_report.results[0]["actual_response"]
