"""Unit tests for utils init."""

import pytest

from imednet import utils


def test_lazy_load_pandas_functions() -> None:
    """Test that lazy load pandas functions."""
    func_subj = utils.subjects_to_dataframe
    from imednet.utils.pandas import subjects_to_dataframe as expected_stodf

    assert func_subj is expected_stodf
    func = utils.records_to_dataframe
    from imednet.utils.pandas import records_to_dataframe as expected_rtodf

    assert func is expected_rtodf
    func2 = utils.export_records_csv
    from imednet.utils.pandas import export_records_csv as expected_export

    assert func2 is expected_export


def test_lazy_load_arrow_function() -> None:
    """Test that lazy load arrow function."""
    func = utils.to_arrow_table
    from imednet.utils.arrow import to_arrow_table as expected_to_arrow_table

    assert func is expected_to_arrow_table


def test_schema_objects_not_in_utils() -> None:
    """Test that schema objects not in utils."""
    with pytest.raises(AttributeError):
        utils.SchemaCache
    with pytest.raises(AttributeError):
        utils.SchemaValidator
    with pytest.raises(AttributeError):
        utils.validate_record_data


def test_getattr_unknown() -> None:
    """Test that getattr unknown."""
    with pytest.raises(AttributeError):
        utils.does_not_exist
