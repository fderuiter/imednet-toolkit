"""Tests for backward-compatible data dictionary models."""

from __future__ import annotations

from unittest.mock import Mock

from imednet.datadict.models import DataDictionary
from imednet_workflows.uat.engine import UATExecutionEngine


def test_legacy_data_dictionary_uat_engine_compat() -> None:
    """Test DataDictionary initialized with legacy format works seamlessly with UATExecutionEngine."""
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
    assert len(engine._rules) == 1
    assert engine._rules[0]["rule_name"] == "Rule1"
    neg = engine.generate_negative_test_case(engine._rules[0])
    assert neg["V1"] == "17.0"
