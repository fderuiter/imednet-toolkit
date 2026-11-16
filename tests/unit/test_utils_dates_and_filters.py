"""Unit tests for utils dates and filters."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from imednet.endpoints.records import RecordsEndpoint
from imednet.utils.dates import format_iso_datetime, parse_iso_datetime
from imednet.utils.filters import (
    ALLOWED_OPERATORS,
    build_filter_string,
    validate_connector_exclusivity,
    validate_filter_key,
    validate_filter_operator,
    validate_filter_syntax,
    validate_record_filter,
)


def test_parse_iso_datetime_with_z() -> None:
    """Test that parse iso datetime with z."""
    dt = parse_iso_datetime("2024-01-01T12:30:00Z")
    assert dt == datetime(2024, 1, 1, 12, 30, 0, tzinfo=timezone.utc)


def test_parse_iso_datetime_with_offset() -> None:
    """Test that parse iso datetime with offset."""
    dt = parse_iso_datetime("2024-01-01T12:30:00+02:00")
    assert dt.hour == 12
    assert dt.tzinfo is not None
    assert dt.tzinfo.utcoffset(dt) == timedelta(hours=2)


def test_parse_iso_datetime_naive() -> None:
    """Test that parse iso datetime naive."""
    dt = parse_iso_datetime("2024-01-01T12:30:00")
    assert dt.tzinfo is None
    assert dt.year == 2024


def test_parse_iso_datetime_millis_padding() -> None:
    """Test that parse iso datetime millis padding."""
    dt = parse_iso_datetime("2021-12-09T08:23:21.99")
    assert dt.microsecond == 990000
    assert dt.tzinfo is None


def test_parse_iso_datetime_micro_padding() -> None:
    """Test that parse iso datetime micro padding."""
    dt = parse_iso_datetime("2025-06-30T21:40:44.98268")
    assert dt.microsecond == 982680


def test_parse_iso_datetime_invalid() -> None:
    """Test that parse iso datetime invalid."""
    with pytest.raises(ValueError):
        parse_iso_datetime("not-a-date")


def test_parse_iso_datetime_none() -> None:
    """Test that parse iso datetime none."""
    with pytest.raises((AttributeError, TypeError)):
        parse_iso_datetime(None)  # type: ignore[arg-type]


def test_format_iso_datetime_aware() -> None:
    """Test that format iso datetime aware."""
    dt = datetime(2024, 1, 1, 12, 30, 0, tzinfo=timezone(timedelta(hours=2)))
    result = format_iso_datetime(dt)
    assert result == "2024-01-01T10:30:00Z"


def test_format_iso_datetime_naive() -> None:
    """Test that format iso datetime naive."""
    dt = datetime(2024, 1, 1, 12, 30, 0)
    result = format_iso_datetime(dt)
    assert result == "2024-01-01T12:30:00Z"


def test_build_filter_string_simple() -> None:
    """Test that build filter string simple."""
    result = build_filter_string({"name": "Alice"})
    assert result == "name==Alice"


def test_build_filter_string_multiple() -> None:
    """Test that build filter string multiple."""
    result = build_filter_string({"name": "A", "age": 30})
    assert result == "name==A;age==30"


def test_build_filter_string_tuple_and_list_raises_on_mixed_connectors() -> None:
    """Test that build filter string raises ValueError when combining tuple and list."""
    with pytest.raises(ValueError, match="cannot combine both ';' \\(AND\\) and ',' \\(OR\\)"):
        build_filter_string({"age": (">", 20), "type": ["A", "B"]})


def test_build_filter_string_bool_and_none() -> None:
    """Test that build filter string bool and none."""
    result = build_filter_string({"active": True, "missing": None})
    assert result == "active==True;missing==None"


def test_build_filter_string_empty() -> None:
    """Test that build filter string empty."""
    assert build_filter_string({}) == ""


def test_build_filter_string_snake_to_camel() -> None:
    """Test that build filter string snake to camel."""
    result = build_filter_string({"form_name": "Demo", "visit_date": (">=", "2024")})
    assert result == "formName==Demo;visitDate>=2024"


def test_build_filter_string_snake_list() -> None:
    """Test that build filter string snake list."""
    result = build_filter_string({"field_name": ["A", "B"]})
    assert result == "fieldName==A,fieldName==B"


def test_build_filter_string_quotes() -> None:
    """Test that build filter string quotes."""
    result = build_filter_string({"site_name": "My Site"})
    assert result == 'siteName=="My Site"'


def test_build_filter_string_quote_spaces() -> None:
    """Test that build filter string quote spaces."""
    result = build_filter_string({"site_name": "A B"})
    assert result == 'siteName=="A B"'


def test_build_filter_string_backslashes() -> None:
    """Test that build filter string backslashes."""
    result = build_filter_string({"path": r"C:\Temp", "quote": r"A\"B"})
    # path: C:\Temp -> "C:\\Temp"
    # quote: A\"B -> "A\\\"B"
    assert result == r'path=="C:\\Temp";quote=="A\\\"B"'


def test_allowed_operators_constant() -> None:
    """Test that ALLOWED_OPERATORS matches expected operator tokens."""
    assert ALLOWED_OPERATORS == frozenset({"<", "<=", ">", ">=", "==", "!=", "=~"})


@pytest.mark.parametrize("op", ["<", "<=", ">", ">=", "==", "!=", "=~"])
def test_valid_operators(op: str) -> None:
    """Test all allowed operators."""
    result = build_filter_string({"field": (op, "value")})
    assert result == f"field{op}value"


@pytest.mark.parametrize("invalid_op", ["===", "<>", "LIKE", "in", "contains", "!~"])
def test_invalid_operators(invalid_op: str) -> None:
    """Test that invalid operators raise ValueError."""
    with pytest.raises(ValueError, match="Invalid filter operator"):
        build_filter_string({"field": (invalid_op, "value")})


@pytest.mark.parametrize("whitespace_op", [" >", "> ", " > ", "\t>="])
def test_whitespace_in_operator_raises(whitespace_op: str) -> None:
    """Test that operators with whitespace raise ValueError."""
    with pytest.raises(ValueError, match="cannot contain whitespace"):
        build_filter_string({"field": (whitespace_op, "value")})


@pytest.mark.parametrize("invalid_key", ["field name", " field", "field ", "field\tname"])
def test_whitespace_in_key_raises(invalid_key: str) -> None:
    """Test that keys with whitespace raise ValueError."""
    with pytest.raises(ValueError, match="cannot contain whitespace"):
        build_filter_string({invalid_key: "value"})


def test_validate_filter_operator_type_error() -> None:
    """Test that non-string operators raise ValueError."""
    with pytest.raises(ValueError, match="non-empty string"):
        validate_filter_operator(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty string"):
        validate_filter_operator("")


def test_validate_filter_key_type_error() -> None:
    """Test that non-string or empty keys raise ValueError."""
    with pytest.raises(ValueError, match="non-empty string"):
        validate_filter_key("")
    with pytest.raises(ValueError, match="non-empty string"):
        validate_filter_key("   ")


def test_invalid_tuple_length_raises() -> None:
    """Test that filter tuples of length other than 2 raise ValueError."""
    with pytest.raises(ValueError, match="must have exactly 2 elements"):
        build_filter_string({"field": (">",)})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must have exactly 2 elements"):
        build_filter_string({"field": (">", 10, "extra")})  # type: ignore[arg-type]


def test_invalid_value_type_raises() -> None:
    """Test that dictionary or set filter values raise ValueError."""
    with pytest.raises(ValueError, match="Invalid filter value type"):
        build_filter_string({"field": {"invalid": "dict"}})


def test_connector_exclusivity_single_connectors() -> None:
    """Test that expressions with only semicolon or only comma are accepted."""
    validate_connector_exclusivity("a==1;b==2;c==3")
    validate_connector_exclusivity("a==1,b==2,c==3")
    validate_connector_exclusivity("singleCondition==val")
    validate_connector_exclusivity("")


def test_connector_exclusivity_mixed_raises() -> None:
    """Test that mixing semicolon and comma raises ValueError."""
    with pytest.raises(ValueError, match="cannot combine both ';' \\(AND\\) and ',' \\(OR\\)"):
        validate_connector_exclusivity("a==1;b==2,c==3")


def test_connector_exclusivity_quoted_connectors() -> None:
    """Test that connectors inside quotation marks are ignored for exclusivity."""
    validate_connector_exclusivity('name=="Smith, John";status==active')
    validate_connector_exclusivity('name=="Smith; John",status==active')
    validate_connector_exclusivity(r'name=="Escaped \"quote, still";status==1')


def test_connector_exclusivity_unclosed_quotes_raises() -> None:
    """Test that unclosed quotes raise ValueError."""
    with pytest.raises(ValueError, match="unclosed quotation marks"):
        validate_connector_exclusivity('name=="Smith, John')


@pytest.mark.parametrize("none_val", ["none", "None", "NONE", "  none  "])
def test_connector_and_record_filter_none_bypass(none_val: str) -> None:
    """Test that literal 'none' bypasses connector and syntax validation."""
    validate_connector_exclusivity(none_val)
    validate_record_filter(none_val)
    validate_filter_syntax(none_val)


@pytest.mark.parametrize("expr", ["age > 10", "age> 10", "age >10", " age>10", "age>10 "])
def test_filter_syntax_whitespace_outside_quotes_raises(expr: str) -> None:
    """Test that whitespace outside quotes raises ValueError."""
    with pytest.raises(ValueError, match="contains whitespace"):
        validate_filter_syntax(expr)


def test_filter_syntax_valid() -> None:
    """Test valid filter syntax."""
    validate_filter_syntax("age>10")
    validate_filter_syntax('siteName=="Bright Test Site"')
    validate_filter_syntax('siteName=="Bright Test Site";status==active')


def test_endpoint_resolve_params_valid_record_filter() -> None:
    """Test that endpoint resolves params with valid recordDataFilter."""
    ep = RecordsEndpoint(MagicMock())
    state = ep._resolve_params(
        study_key="S1",
        extra_params={"recordDataFilter": "age>10"},
        filters={},
    )
    assert state.params["recordDataFilter"] == "age>10"


def test_endpoint_resolve_params_none_record_filter() -> None:
    """Test that endpoint resolves params with 'none' recordDataFilter bypass."""
    ep = RecordsEndpoint(MagicMock())
    state = ep._resolve_params(
        study_key="S1",
        extra_params={"recordDataFilter": "none"},
        filters={},
    )
    assert state.params["recordDataFilter"] == "none"


def test_endpoint_resolve_params_quoted_comma_record_filter() -> None:
    """Test that endpoint accepts recordDataFilter with comma inside quotes."""
    ep = RecordsEndpoint(MagicMock())
    state = ep._resolve_params(
        study_key="S1",
        extra_params={"recordDataFilter": 'desc=="a,b";status==1'},
        filters={},
    )
    assert state.params["recordDataFilter"] == 'desc=="a,b";status==1'


def test_endpoint_resolve_params_mixed_connectors_record_filter_raises() -> None:
    """Test that endpoint parameter resolution rejects mixed connectors in recordDataFilter."""
    ep = RecordsEndpoint(MagicMock())
    with pytest.raises(ValueError, match="cannot combine both ';' \\(AND\\) and ',' \\(OR\\)"):
        ep._resolve_params(
            study_key="S1",
            extra_params={"recordDataFilter": "a==1;b==2,c==3"},
            filters={},
        )


def test_endpoint_resolve_params_whitespace_record_filter_raises() -> None:
    """Test that endpoint parameter resolution rejects whitespace in recordDataFilter."""
    ep = RecordsEndpoint(MagicMock())
    with pytest.raises(ValueError, match="contains whitespace"):
        ep._resolve_params(
            study_key="S1",
            extra_params={"recordDataFilter": "a == 1"},
            filters={},
        )


def test_endpoint_resolve_params_special_param_record_data_filter() -> None:
    """Test that endpoint parameter resolution validates record_data_filter from filters."""
    ep = RecordsEndpoint(MagicMock())
    with pytest.raises(ValueError, match="cannot combine both ';' \\(AND\\) and ',' \\(OR\\)"):
        ep._resolve_params(
            study_key="S1",
            extra_params=None,
            filters={"record_data_filter": "a==1;b==2,c==3"},
        )
