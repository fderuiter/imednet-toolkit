"""Tests for backward-compatible shims in imednet.validation.data_dictionary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from imednet.validation.data_dictionary import DataDictionary, DataDictionaryLoader
from imednet_workflows.uat.engine import UATExecutionEngine

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "data_dictionary"


def test_loader_from_directory_issues_future_warning() -> None:
    """Test that DataDictionaryLoader.from_directory issues a FutureWarning."""
    with pytest.warns(FutureWarning, match="DataDictionaryLoader is deprecated"):
        dd = DataDictionaryLoader.from_directory(FIXTURES_DIR)
    assert isinstance(dd, DataDictionary)
    assert dd.forms[0]["Form Key"] == "AE"


def test_loader_from_files_issues_future_warning() -> None:
    """Test that DataDictionaryLoader.from_files issues a FutureWarning."""
    with pytest.warns(FutureWarning, match="DataDictionaryLoader is deprecated"):
        dd = DataDictionaryLoader.from_files(
            forms=FIXTURES_DIR / "FORMS.csv",
            questions=FIXTURES_DIR / "QUESTIONS.csv",
            choices=FIXTURES_DIR / "CHOICES.csv",
            business_logic=FIXTURES_DIR / "BUSINESS_LOGIC.csv",
        )
    assert isinstance(dd, DataDictionary)
    assert len(dd.forms) == 3


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
