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
