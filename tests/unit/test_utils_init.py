"""Unit tests for utils init."""

import pytest

from imednet import utils


def test_lazy_load_pandas_functions() -> None:
    """Test that lazy load pandas functions."""
    func_query = utils.queries_to_dataframe
    from imednet.utils.pandas import queries_to_dataframe as expected_qtodf

    assert func_query is expected_qtodf
    func_subj = utils.subjects_to_dataframe
    from imednet.utils.pandas import subjects_to_dataframe as expected_stodf

    assert func_subj is expected_stodf
    func_var = utils.variables_to_dataframe
    from imednet.utils.pandas import variables_to_dataframe as expected_vtodf

    assert func_var is expected_vtodf
    func_form = utils.forms_to_dataframe
    from imednet.utils.pandas import forms_to_dataframe as expected_ftodf

    assert func_form is expected_ftodf
    func_visit = utils.visits_to_dataframe
    from imednet.utils.pandas import visits_to_dataframe as expected_vtodf_visits

    assert func_visit is expected_vtodf_visits
    func_interval = utils.intervals_to_dataframe
    from imednet.utils.pandas import intervals_to_dataframe as expected_itodf

    assert func_interval is expected_itodf
    func_site = utils.sites_to_dataframe
    from imednet.utils.pandas import sites_to_dataframe as expected_sitedf

    assert func_site is expected_sitedf
    func = utils.records_to_dataframe
    from imednet.utils.pandas import records_to_dataframe as expected_rtodf

    assert func is expected_rtodf
    func2 = utils.export_records_csv
    from imednet.utils.pandas import export_records_csv as expected_export

    assert func2 is expected_export
    func_check = utils.check_pandas
    from imednet.utils.pandas import check_pandas as expected_check_pandas

    assert func_check is expected_check_pandas


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
