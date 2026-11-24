"""Unit tests for utils dates and filters."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from imednet.endpoints.records import RecordsEndpoint
from imednet.models.visits import Visit
from imednet.spi.utils import (
    ORDERED_FILTER_OPERATORS,
    normalize_utc_timestamp_bound,
    parse_filter_expression,
    parse_filter_expressions,
)
from imednet.utils.dates import format_iso_datetime, parse_iso_datetime
from imednet.utils.filters import (
    ALLOWED_OPERATORS,
    RECORD_DATA_OPERATORS,
    build_filter_string,
    extract_filter_attributes,
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


@pytest.mark.parametrize(
    ("raw", "expected_microsecond", "expected_offset"),
    [
        ("2024-01-01T12:00:00.1", 100000, None),
        ("2024-01-01T12:00:00.12+02:00", 120000, timedelta(hours=2)),
        ("2024-01-01T12:00:00.123456", 123456, None),
        ("2024-01-01T12:00:00.1234567", 123456, None),
        ("2024-01-01T12:00:00.123456789Z", 123456, timedelta(0)),
    ],
)
def test_parse_iso_datetime_fractional_boundaries_and_offsets(
    raw: str,
    expected_microsecond: int,
    expected_offset: timedelta | None,
) -> None:
    """Test fractional-second padding, microsecond truncation, and timezone offsets."""
    dt = parse_iso_datetime(raw)
    assert dt.microsecond == expected_microsecond
    if expected_offset is None:
        assert dt.tzinfo is None
    elif expected_offset == timedelta(0):
        assert dt.tzinfo == timezone.utc
    else:
        assert dt.tzinfo is not None
        assert dt.tzinfo.utcoffset(dt) == expected_offset


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
    """Test lowercase boolean serialization and ValueError on None values."""
    result = build_filter_string({"active": True, "deleted": False})
    assert result == "active==true;deleted==false"

    with pytest.raises(ValueError, match="cannot be None"):
        build_filter_string({"missing": None})
    with pytest.raises(ValueError, match="cannot be None"):
        build_filter_string({"missing": ("==", None)})
    with pytest.raises(ValueError, match="cannot be None"):
        build_filter_string({"missing": ["A", None]})


def test_build_filter_string_unquoted_iso_timestamps_and_dates() -> None:
    """Test ISO-8601 timestamps and date/datetime objects serialize unquoted."""
    dt = datetime(2024, 1, 2, 12, 30, 0, tzinfo=timezone.utc)
    d = date(2024, 1, 2)
    assert (
        build_filter_string({"date_modified": (">=", dt)}) == "dateModified>=2024-01-02T12:30:00Z"
    )
    assert build_filter_string({"visit_date": d}) == "visitDate==2024-01-02"
    assert (
        build_filter_string({"date_modified": (">=", "2024-01-02T00:00:00Z")})
        == "dateModified>=2024-01-02T00:00:00Z"
    )
    assert (
        build_filter_string({"date_created": ("<", "2024-01-02T00:00:00-06:00")})
        == "dateCreated<2024-01-02T00:00:00-06:00"
    )


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
    """Test that ALLOWED_OPERATORS excludes =~ while RECORD_DATA_OPERATORS includes =~."""
    assert ALLOWED_OPERATORS == frozenset({"<", "<=", ">", ">=", "==", "!="})
    assert RECORD_DATA_OPERATORS == frozenset({"<", "<=", ">", ">=", "==", "!=", "=~"})


@pytest.mark.parametrize("op", ["<", "<=", ">", ">=", "==", "!="])
def test_valid_operators(op: str) -> None:
    """Test all allowed standard filter operators."""
    result = build_filter_string({"field": (op, "value")})
    assert result == f"field{op}value"


def test_regex_operator_restricted_to_record_data_filter() -> None:
    """Test =~ is rejected in standard filters and accepted in recordDataFilter."""
    with pytest.raises(ValueError, match="Invalid filter operator '=~'"):
        build_filter_string({"field": ("=~", "value")})
    with pytest.raises(ValueError, match="Invalid filter operator '=~'"):
        validate_filter_syntax("field=~value")

    validate_filter_operator("=~", allow_regex=True)
    validate_record_filter("aeTerm=~headache")


def test_duplicate_attributes_rejected_in_and_clauses_allowed_in_or_clauses() -> None:
    """Test duplicate attributes in ';' AND clauses raise ValueError while ',' OR allows repeats."""
    with pytest.raises(ValueError, match="Duplicate filter attribute 'siteId'"):
        build_filter_string({"site_id": 1, "siteId": 2})

    with pytest.raises(ValueError, match="Duplicate filter attribute 'name'"):
        validate_filter_syntax("name==John;name!=Jane")

    with pytest.raises(ValueError, match="Duplicate filter attribute 'age'"):
        validate_record_filter("age>=18;age<=65")

    validate_filter_syntax("subjectKey==SUB1,subjectKey==SUB2")
    validate_record_filter("aeTerm==A,aeTerm==B")
    assert extract_filter_attributes("subjectKey==SUB1,subjectKey==SUB2") == [
        "subjectKey",
        "subjectKey",
    ]


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


@pytest.mark.parametrize("bad_key", ["revision>", "revision<", "status!", "key=1", "name~"])
def test_validate_filter_key_rejects_operator_chars(bad_key: str) -> None:
    """Test that validate_filter_key rejects operator characters in keys."""
    with pytest.raises(ValueError, match="cannot contain operator characters"):
        validate_filter_key(bad_key)


def test_validate_filter_key_model_attributes() -> None:
    """Test model-backed filter key validation for snake_case and camelCase attributes."""
    validate_filter_key("interval_name", model=Visit)
    validate_filter_key("intervalName", model=Visit)
    validate_filter_key("visit_id", model=Visit)
    validate_filter_key("visitId", model=Visit)
    with pytest.raises(ValueError, match="Unknown filter attribute 'visit_key' for Visit"):
        validate_filter_key("visit_key", model=Visit)


def test_ordered_filter_operators_and_expression_parsing() -> None:
    """Test operator-aware filter expression parsing across all documented operators."""
    assert ORDERED_FILTER_OPERATORS == ("<=", ">=", "!=", "==", "=~", "<", ">")
    assert parse_filter_expression("dictionaryName==MedDRA") == (
        "dictionaryName",
        ("==", "MedDRA"),
    )
    assert parse_filter_expression("revision>=2") == ("revision", (">=", 2))
    assert parse_filter_expression("revision<=5") == ("revision", ("<=", 5))
    assert parse_filter_expression("subjectStatus!=Enrolled") == (
        "subjectStatus",
        ("!=", "Enrolled"),
    )
    with pytest.raises(
        ValueError, match=r"Allowed operators: \['!=', '<', '<=', '==', '>', '>='\]"
    ):
        parse_filter_expression("subjectKey=~^SUBJ.*")
    assert parse_filter_expression("subjectKey=~^SUBJ.*", allow_regex=True) == (
        "subjectKey",
        ("=~", "^SUBJ.*"),
    )
    assert parse_filter_expression("revision>1") == ("revision", (">", 1))
    assert parse_filter_expression("revision<10") == ("revision", ("<", 10))
    assert parse_filter_expression("interval_name=Screening", model=Visit) == (
        "interval_name",
        "Screening",
    )

    with pytest.raises(ValueError, match="Unknown filter attribute 'visit_key' for Visit"):
        parse_filter_expression("visit_key=SCREENING", model=Visit)

    for malformed in ["", "   ", "no_op", "=val", "key=", "a===b", "a<>b", "field~=val"]:
        with pytest.raises(ValueError):
            parse_filter_expression(malformed)


def test_build_filter_string_rejects_duplicate_operator_tuples_in_and_clause() -> None:
    """Test both CLI and programmatic filter builders reject duplicate attributes in AND clauses."""
    with pytest.raises(ValueError, match="Duplicate filter attribute 'dateCreated' in AND clause"):
        parse_filter_expressions(
            [
                "dateCreated>=2026-01-01T00:00:00Z",
                "dateCreated<=2026-02-01T23:59:59Z",
                "studyKey==STUDY",
            ]
        )

    programmatic_bounds = {
        "dateCreated": [
            (">=", "2026-01-01T00:00:00Z"),
            ("<=", "2026-02-01T23:59:59Z"),
        ],
        "studyKey": ("==", "STUDY"),
    }
    with pytest.raises(ValueError, match="Duplicate filter attribute 'dateCreated' in AND clause"):
        build_filter_string(programmatic_bounds)

    # Single-element operator tuple list produces a valid single clause
    assert (
        build_filter_string(
            {
                "dateCreated": [(">=", "2026-01-01T00:00:00Z")],
                "studyKey": ("==", "STUDY"),
            }
        )
        == "dateCreated>=2026-01-01T00:00:00Z;studyKey==STUDY"
    )

    with pytest.raises(ValueError, match="cannot mix operator tuples and scalar values"):
        build_filter_string({"age": [(">", 18), 30]})

    with pytest.raises(ValueError, match="must have exactly 2 elements"):
        build_filter_string({"age": [(">", 18, "bad")]})  # type: ignore[list-item]


def test_normalize_utc_timestamp_bound() -> None:
    """Test date-only and timestamp normalization for UTC audit bounds."""
    assert normalize_utc_timestamp_bound("2026-01-01", is_end=False) == "2026-01-01T00:00:00Z"
    assert normalize_utc_timestamp_bound("2026-02-01", is_end=True) == "2026-02-01T23:59:59Z"
    assert (
        normalize_utc_timestamp_bound("2026-01-15T14:30:00+02:00", is_end=False)
        == "2026-01-15T12:30:00Z"
    )
    with pytest.raises(ValueError):
        normalize_utc_timestamp_bound("")
    with pytest.raises(ValueError):
        normalize_utc_timestamp_bound("2026-99-99")


def test_model_less_scalar_coercion_unpadded_digits_and_unicode() -> None:
    """When model is None, only unpadded ASCII digits convert to int; padded and Unicode stay str."""
    assert parse_filter_expression("count=0") == ("count", 0)
    assert parse_filter_expression("count=42") == ("count", 42)
    assert parse_filter_expression("subject_key=001") == ("subject_key", "001")
    assert parse_filter_expression("code=0042") == ("code", "0042")
    assert parse_filter_expression('count="42"') == ("count", "42")
    assert parse_filter_expression("value=²") == ("value", "²")
    assert parse_filter_expression("value=³") == ("value", "³")
    assert parse_filter_expression("value=٠١٢") == ("value", "٠١٢")
    assert build_filter_string(parse_filter_expressions(["subject_key=001", "code=0042"])) == (
        "subjectKey==001;code==0042"
    )


def test_schema_guided_scalar_coercion_across_model_types() -> None:
    """Schema-guided coercion preserves text strings and validates int, float, and bool fields."""
    from enum import Enum
    from typing import Annotated, Literal

    from pydantic import BaseModel, Field

    from imednet.models.codings import Coding
    from imednet.models.forms import Form
    from imednet.models.intervals import Interval
    from imednet.models.subjects import Subject

    # String fields preserve leading zeros, numeric text, boolean tokens, and superscripts
    assert parse_filter_expressions(["subject_key=001"], model=Subject) == {"subject_key": "001"}
    assert parse_filter_expressions(["subjectKey=123"], model=Subject) == {"subjectKey": "123"}
    assert parse_filter_expressions(["code=0042"], model=Coding) == {"code": "0042"}
    assert parse_filter_expressions(["value=true"], model=Coding) == {"value": "true"}
    assert parse_filter_expressions(["value=²"], model=Coding) == {"value": "²"}

    # Integer fields coerce ASCII integers (including negative bounds) and reject non-integers
    assert parse_filter_expressions(["revision>=2"], model=Coding) == {"revision": (">=", 2)}
    assert parse_filter_expressions(["negative_slack=-5"], model=Interval) == {"negative_slack": -5}
    for bad_int in ["revision=abc", "revision=²", "revision=1.5", "revision=true"]:
        with pytest.raises(ValueError, match="Invalid integer value"):
            parse_filter_expressions([bad_int], model=Coding)

    # Boolean fields accept true/false case-insensitively and reject invalid tokens
    assert parse_filter_expressions(["disabled=true"], model=Form) == {"disabled": True}
    assert parse_filter_expressions(["disabled=FALSE"], model=Form) == {"disabled": False}
    for bad_bool in ["disabled=abc", "disabled=1", "disabled=yes"]:
        with pytest.raises(ValueError, match="Invalid boolean value"):
            parse_filter_expressions([bad_bool], model=Form)

    # Custom Pydantic annotations (float, Annotated, Literal, plain Enum, validation_alias, Union)
    class PlainColor(Enum):
        RED = 1

    class CustomFilterModel(BaseModel):
        score: Annotated[float | None, Field(validation_alias="scoreVal")] = None
        tier: Literal["gold", "silver"] | None = None
        flag_lit: Literal[True, False] | None = None
        rank_lit: Literal[1, 2] | None = None
        ratio_lit: Literal[0.5, 1.5] | None = None
        color: PlainColor | None = None
        mixed: int | str | None = None

    assert parse_filter_expression("score>=3.5", model=CustomFilterModel) == (
        "score",
        (">=", 3.5),
    )
    assert parse_filter_expression("scoreVal=-0.25", model=CustomFilterModel) == (
        "scoreVal",
        -0.25,
    )
    with pytest.raises(ValueError, match="Invalid numeric value"):
        parse_filter_expression("score=abc", model=CustomFilterModel)

    assert parse_filter_expression("tier=001", model=CustomFilterModel) == ("tier", "001")
    assert parse_filter_expression("flag_lit=true", model=CustomFilterModel) == ("flag_lit", True)
    assert parse_filter_expression("rank_lit=2", model=CustomFilterModel) == ("rank_lit", 2)
    assert parse_filter_expression("ratio_lit=1.5", model=CustomFilterModel) == ("ratio_lit", 1.5)
    assert parse_filter_expression("color=001", model=CustomFilterModel) == ("color", "001")
    assert parse_filter_expression("mixed=42", model=CustomFilterModel) == ("mixed", 42)
    assert parse_filter_expression("mixed=0042", model=CustomFilterModel) == ("mixed", "0042")


def test_validate_filter_key_allowed_attributes_and_sorted_listing() -> None:
    """validate_filter_key enforces endpoint FILTER_ATTRIBUTES and lists valid attributes."""
    from imednet.endpoints.queries import QueriesEndpoint
    from imednet.endpoints.visits import VisitsEndpoint
    from imednet.models.queries import Query

    validate_filter_key(
        "interval_name",
        model=Visit,
        allowed_attributes=VisitsEndpoint.FILTER_ATTRIBUTES,
    )
    validate_filter_key(
        "intervalName",
        model=Visit,
        allowed_attributes=VisitsEndpoint.FILTER_ATTRIBUTES,
    )

    with pytest.raises(ValueError) as visit_exc:
        validate_filter_key(
            "visit_id",
            model=Visit,
            allowed_attributes=VisitsEndpoint.FILTER_ATTRIBUTES,
        )
    assert "Unknown filter attribute 'visit_id' for Visit." in str(visit_exc.value)
    assert "Valid attributes:" in str(visit_exc.value)
    assert "intervalId" in str(visit_exc.value)

    with pytest.raises(ValueError) as query_exc:
        validate_filter_key(
            "status",
            model=Query,
            allowed_attributes=QueriesEndpoint.FILTER_ATTRIBUTES,
        )
    assert "Unknown filter attribute 'status' for Query." in str(query_exc.value)
    assert "Valid attributes:" in str(query_exc.value)

    with pytest.raises(
        ValueError,
        match="'study_key' and 'studyKey' cannot be used in --filter",
    ):
        validate_filter_key(
            "study_key",
            model=Visit,
            allowed_attributes=VisitsEndpoint.FILTER_ATTRIBUTES,
        )


def test_filter_quote_preservation_on_empty_and_quoted_strings() -> None:
    """Table 10 empty and explicitly quoted strings preserve double quotes on serialization."""
    from imednet.models.codings import Coding

    parsed = parse_filter_expressions(
        ['reason==""', 'code="0042"', 'dictionary_name="MedDRA"'],
        model=Coding,
    )
    reason_op, reason_val = parsed["reason"]
    assert reason_op == "=="
    assert isinstance(reason_val, str)
    assert reason_val == ""
    assert parsed["code"] == "0042"
    assert isinstance(parsed["code"], str)

    serialized = build_filter_string(parsed)
    assert serialized == 'reason=="";code=="0042";dictionaryName=="MedDRA"'

    # Programmatic empty strings in dicts also serialize as ""
    assert build_filter_string({"reason": ("==", "")}) == 'reason==""'
    assert build_filter_string({"reason": ""}) == 'reason==""'

    # OR filter strings also preserve quotes on empty and quoted strings
    or_parsed = parse_filter_expressions(['reason=="",reason=="Not Applicable"'], model=Coding)
    assert or_parsed == {"filter": 'reason=="",reason=="Not Applicable"'}


def test_duplicate_and_attribute_normalization_across_casing_and_syntax() -> None:
    """Normalized camelCase duplicate AND attributes fail while OR and case-sensitive eCRF pass."""
    from imednet.utils.filters import validate_filter_syntax, validate_record_filter

    for bad_exprs in (
        ["record_id>=1", "recordId<=10"],
        ["recordId>=1;recordId<=10"],
        ["record_id>=1;recordId<=10"],
    ):
        with pytest.raises(ValueError, match="Duplicate filter attribute 'recordId'"):
            parse_filter_expressions(bad_exprs)

    with pytest.raises(ValueError, match="Duplicate filter attribute 'recordId'"):
        validate_filter_syntax("record_id>=1;recordId<=10")

    # OR expressions allow the same attribute across comma branches
    assert parse_filter_expressions(["record_id==1,recordId==2"]) == {
        "filter": "recordId==1,recordId==2"
    }

    # Case-sensitive eCRF variable names remain distinct unless identical
    validate_record_filter("age>=18;AGE<=65")
    with pytest.raises(ValueError, match="Duplicate filter attribute 'AGE'"):
        validate_record_filter("AGE>=18;AGE<=65")
