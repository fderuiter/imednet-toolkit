"""Unit tests for record mapper hierarchy."""

from datetime import datetime
from unittest.mock import MagicMock, patch

from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet_workflows.record_mapper import RecordMapper


@patch("imednet_workflows.study_structure.get_study_structure")
def test_build_hierarchy(mock_get_study_structure):
    """Test that build hierarchy."""
    sdk = MagicMock()

    mock_study_struct = MagicMock()
    interval = MagicMock()
    form = MagicMock()
    form.form_id = 10
    var1 = Variable(variable_name="VAR1", label="Label1", form_id=10)
    var2 = Variable(variable_name="VAR2", label="Label2", form_id=10)
    form.variables = [var1, var2]
    interval.forms = [form]
    mock_study_struct.intervals = [interval]
    mock_get_study_structure.return_value = mock_study_struct

    records = [
        Record(
            record_id=1,
            subject_key="S1",
            visit_id=1,
            form_id=10,
            record_status="Complete",
            date_created=datetime(2021, 1, 1),
            record_data={"VAR1": "a", "VAR2": "b"},
        ),
        Record(
            record_id=2,
            subject_key="S1",
            visit_id=2,
            form_id=10,
            record_status="Complete",
            date_created=datetime(2021, 1, 2),
            record_data={"VAR1": "c", "VAR2": "d"},
            parent_record_id=999,
        ),
    ]

    mapper = RecordMapper(sdk)
    mapper._iter_records = MagicMock(return_value=records)

    # Run build_hierarchy with use_labels_as_keys = True
    tree = mapper.build_hierarchy("STUDY", use_labels_as_keys=True)

    assert len(tree) == 1
    subj = tree[0]
    assert subj["subject_key"] == "S1"
    assert len(subj["visits"]) == 2

    v1 = subj["visits"][0]
    assert v1["visit_id"] == 1
    assert len(v1["forms"]) == 1

    f10 = v1["forms"][0]
    assert f10["form_id"] == 10
    assert len(f10["records"]) == 1
    rec1 = f10["records"][0]
    assert rec1["Label1"] == "a"
    assert rec1["Label2"] == "b"
    assert rec1["parent_record_id"] == "1_10"

    v2 = subj["visits"][1]
    f10_2 = v2["forms"][0]
    rec2 = f10_2["records"][0]
    assert rec2["Label1"] == "c"
    assert rec2["Label2"] == "d"
    assert rec2["parent_record_id"] == 999


@patch("imednet_workflows.study_structure.get_study_structure")
def test_build_hierarchy_deduplicates_form_model_synthesis(mock_get_study_structure):
    """Test that _build_record_model is called only once per unique form across multiple visit intervals."""
    sdk = MagicMock()

    mock_study_struct = MagicMock()

    # Form 10 appears in both Interval 1 and Interval 2
    form10 = MagicMock()
    form10.form_id = 10
    var1 = Variable(variable_name="VAR1", label="Label1", form_id=10)
    var2 = Variable(variable_name="VAR2", label="Label2", form_id=10)
    form10.variables = [var1, var2]

    # Form 20 appears only in Interval 1
    form20 = MagicMock()
    form20.form_id = 20
    var3 = Variable(variable_name="VAR3", label="Label3", form_id=20)
    form20.variables = [var3]

    # Form 30 appears only in Interval 2
    form30 = MagicMock()
    form30.form_id = 30
    var4 = Variable(variable_name="VAR4", label="Label4", form_id=30)
    form30.variables = [var4]

    interval1 = MagicMock()
    interval1.forms = [form10, form20]

    interval2 = MagicMock()
    interval2.forms = [form10, form30]

    mock_study_struct.intervals = [interval1, interval2]
    mock_get_study_structure.return_value = mock_study_struct

    records = [
        Record(
            record_id=1,
            subject_key="S1",
            visit_id=1,
            form_id=10,
            record_status="Complete",
            date_created=datetime(2021, 1, 1),
            record_data={"VAR1": "val1", "VAR2": "val2"},
        ),
        Record(
            record_id=2,
            subject_key="S1",
            visit_id=2,
            form_id=10,
            record_status="Complete",
            date_created=datetime(2021, 1, 2),
            record_data={"VAR1": "val3", "VAR2": "val4"},
        ),
        Record(
            record_id=3,
            subject_key="S1",
            visit_id=1,
            form_id=20,
            record_status="Complete",
            date_created=datetime(2021, 1, 3),
            record_data={"VAR3": "val5"},
        ),
        Record(
            record_id=4,
            subject_key="S1",
            visit_id=2,
            form_id=30,
            record_status="Complete",
            date_created=datetime(2021, 1, 4),
            record_data={"VAR4": "val6"},
        ),
    ]

    mapper = RecordMapper(sdk)
    mapper._iter_records = MagicMock(return_value=records)
    spy_build_record_model = MagicMock(wraps=mapper._build_record_model)
    mapper._build_record_model = spy_build_record_model

    tree = mapper.build_hierarchy("STUDY", use_labels_as_keys=False)

    # 3 unique forms (10, 20, 30), so _build_record_model should only be called 3 times, not 4
    assert spy_build_record_model.call_count == 3

    assert len(tree) == 1
    subj = tree[0]
    assert subj["subject_key"] == "S1"
    assert len(subj["visits"]) == 2

    # Verify both records with form_id=10 were parsed properly
    v1 = next(v for v in subj["visits"] if v["visit_id"] == 1)
    f10_v1 = next(f for f in v1["forms"] if f["form_id"] == 10)
    assert f10_v1["records"][0]["VAR1"] == "val1"

    v2 = next(v for v in subj["visits"] if v["visit_id"] == 2)
    f10_v2 = next(f for f in v2["forms"] if f["form_id"] == 10)
    assert f10_v2["records"][0]["VAR1"] == "val3"


@patch("imednet_workflows.study_structure.get_study_structure")
def test_build_hierarchy_empty_variables(mock_get_study_structure):
    """Test that forms without variables synthesize empty models without error."""
    sdk = MagicMock()

    mock_study_struct = MagicMock()
    interval = MagicMock()
    form = MagicMock()
    form.form_id = 50
    form.variables = []
    interval.forms = [form]
    mock_study_struct.intervals = [interval]
    mock_get_study_structure.return_value = mock_study_struct

    records = [
        Record(
            record_id=101,
            subject_key="S2",
            visit_id=1,
            form_id=50,
            record_status="Complete",
            date_created=datetime(2021, 1, 1),
            record_data={},
        )
    ]

    mapper = RecordMapper(sdk)
    mapper._iter_records = MagicMock(return_value=records)

    tree = mapper.build_hierarchy("STUDY")
    assert len(tree) == 1
    assert tree[0]["visits"][0]["forms"][0]["records"][0]["parent_record_id"] == "1_50"


@patch("imednet_workflows.study_structure.get_study_structure")
def test_build_hierarchy_form_whitelist_filtering(mock_get_study_structure):
    """Test that excluded forms never have models synthesized."""
    sdk = MagicMock()

    mock_study_struct = MagicMock()
    interval = MagicMock()
    form1 = MagicMock()
    form1.form_id = 10
    form1.variables = [Variable(variable_name="V1", label="L1", form_id=10)]
    form2 = MagicMock()
    form2.form_id = 20
    form2.variables = [Variable(variable_name="V2", label="L2", form_id=20)]
    interval.forms = [form1, form2]
    mock_study_struct.intervals = [interval]
    mock_get_study_structure.return_value = mock_study_struct

    mapper = RecordMapper(sdk)
    mapper._iter_records = MagicMock(return_value=[])
    spy_build_record_model = MagicMock(wraps=mapper._build_record_model)
    mapper._build_record_model = spy_build_record_model

    mapper.build_hierarchy("STUDY", form_whitelist=[10])

    assert spy_build_record_model.call_count == 1
