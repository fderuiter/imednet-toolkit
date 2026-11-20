"""Showcase Data Dictionary loaders, ODM XML conversion, diffing, and rule validation."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from imednet import ImednetSDK
from imednet.datadict import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    LogicTreeEvaluator,
    Metadata,
    Variable,
    diff,
    get_schema,
    load_from_api,
    load_from_cdisc_odm,
    load_from_csv_files,
    load_from_redcap,
    parse_logic_xml,
    to_json,
    to_odm_xml,
    validate,
)
from imednet.errors import ValidationError
from imednet.simulation import SimulationState, SimulationTransport
from imednet_workflows.record_update import RecordUpdateWorkflow

LOGIC_XML = (
    "<rule>"
    '<conditions><condition variable="V_AGE" operator="&lt;" value="18"/></conditions>'
    '<actions><action type="error" message="Enrolled Subjects must be 18 or older."/></actions>'
    "</rule>"
)


def _write_sample_exports(work_dir: Path) -> tuple[Path, Path, Path, Path, Path]:
    """Write sample 4-file iMednet CSV exports and a REDCap CSV dictionary."""
    forms_csv = work_dir / "FORMS.csv"
    questions_csv = work_dir / "QUESTIONS.csv"
    choices_csv = work_dir / "CHOICES.csv"
    logic_csv = work_dir / "BUSINESS_LOGIC.csv"
    redcap_csv = work_dir / "redcap_datadict.csv"

    forms_csv.write_text(
        "Form Key,Form Name,Form ID,Form Type\n"
        "F_DM,Demographics,301,Enrollment\n"
        "F_VS,Vital Signs,302,Subject\n",
        encoding="utf-8",
    )
    questions_csv.write_text(
        "Form Key,Variable Name,Label,Variable Type,Sequence,Required Field,Minimum,Maximum\n"
        "F_DM,V_AGE,Age at Consent,Integer,1,Yes,18,95\n"
        "F_DM,V_GENDER,Gender,Radio,2,Yes,,\n"
        "F_VS,V_WEIGHT,Weight (kg),Float,1,Yes,35,200\n",
        encoding="utf-8",
    )
    choices_csv.write_text(
        "Form Key,Variable Name,Choice Value,Choice Text,Position\n"
        "F_DM,V_GENDER,Female,Female,1\n"
        "F_DM,V_GENDER,Male,Male,2\n",
        encoding="utf-8",
    )
    logic_csv.write_text(
        "ID,Name,Type,Status,Form,Sequence,Logic\n"
        "101,Weight Positive Check,Form,Active,F_VS,1,"
        '"<rule><conditions><condition variable=""V_WEIGHT"" operator=""&lt;="" value=""0""/></conditions>'
        '<actions><action type=""error"" message=""Weight must exceed 0 kg.""/></actions></rule>"\n',
        encoding="utf-8",
    )
    redcap_csv.write_text(
        "Variable / Field Name,Form Name,Field Type,Field Label,"
        '"Choices, Calculations, or Slider Labels",Text Validation Type OR Show Slider Number,'
        "Text Validation Min,Text Validation Max,Required Field?\n"
        "v_age,demographics,text,Age at Consent,,integer,18,95,y\n"
        'v_gender,demographics,radio,Gender,"Female, Female | Male, Male",,,,y\n',
        encoding="utf-8",
    )
    return forms_csv, questions_csv, choices_csv, logic_csv, redcap_csv


def main() -> None:
    """Run offline Data Dictionary conversion, diffing, and Record rule validation."""
    with TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        forms_csv, questions_csv, choices_csv, logic_csv, redcap_csv = _write_sample_exports(
            work_dir
        )

        # 1. Load from 4-file CSV export and validate canonical JSON Schema
        csv_dd = load_from_csv_files(
            forms_file=forms_csv,
            questions_file=questions_csv,
            choices_file=choices_csv,
            business_logic_file=logic_csv,
            study_key="STUDY",
        )
        schema_doc = get_schema()
        schema_errors = validate(to_json(csv_dd), schema=schema_doc)
        assert schema_errors == []
        print(
            f"Loaded CSV Data Dictionary for {csv_dd.metadata.studyKey}: "
            f"{len(csv_dd.forms)} Forms (schema version: {schema_doc.get('version')})"
        )

        # 2. Round-trip through CDISC ODM 1.3 XML and load REDCap CSV
        odm_xml = to_odm_xml(csv_dd, file_oid="ODM.STUDY.001")
        odm_path = work_dir / "study.odm.xml"
        odm_path.write_text(odm_xml, encoding="utf-8")
        odm_dd = load_from_cdisc_odm(odm_path, study_key="STUDY")
        redcap_dd = load_from_redcap(redcap_csv, study_key="STUDY")
        print(
            f"ODM round-trip Forms: {len(odm_dd.forms)} | "
            f"REDCap imported Forms: {len(redcap_dd.forms)}"
        )

        # 3. Extract baseline Study metadata from simulated REST API and diff against CSV
        with ImednetSDK(
            simulation_mode=True,
            transport=SimulationTransport(SimulationState()),
        ) as sdk:
            api_dd = load_from_api(sdk, "STUDY")
            diff_report = diff(api_dd, csv_dd)
            print(
                f"Diff API vs CSV (has_changes={diff_report.has_changes}): "
                f"{len(diff_report.items)} structural changes"
            )

            # 4. Evaluate conditional XML rules with LogicTreeEvaluator
            adult_rule = BusinessLogicRule(
                id="201",
                name="Adult Enrollment Rule",
                type="Form",
                status="Active",
                form="F_DM",
                logic=parse_logic_xml(LOGIC_XML),
            )
            evaluator = LogicTreeEvaluator()
            rule_report = evaluator.evaluate_rules(
                [adult_rule],
                {"V_AGE": 16, "V_GENDER": "Female"},
                form_key="F_DM",
            )
            print(f"Offline XML rule violations caught: {len(rule_report.errors)}")

            # 5. Enforce DataDictionary constraints and XML rules on Record submission
            strict_dd = DataDictionary(
                metadata=Metadata.create(study_key="STUDY"),
                forms=[
                    Form(
                        formKey="F_DM",
                        formName="Demographics",
                        variables=[
                            Variable(
                                variableName="V_AGE",
                                variableType="Integer",
                                required="Yes",
                                extraAttributes={"minValue": 18, "maxValue": 95},
                            ),
                            Variable(
                                variableName="V_GENDER",
                                variableType="Radio",
                                required="Yes",
                                choices=[
                                    Choice(choiceValue="Female", choiceText="Female"),
                                    Choice(choiceValue="Male", choiceText="Male"),
                                ],
                            ),
                        ],
                        businessLogic=[adult_rule],
                    ),
                    Form(
                        formKey="F_VS",
                        formName="Vital Signs",
                        variables=[
                            Variable(
                                variableName="V_WEIGHT",
                                variableType="Float",
                                required="Yes",
                                extraAttributes={"minValue": 35.0, "maxValue": 200.0},
                            )
                        ],
                    ),
                ],
            )

            job = sdk.records.create(
                "STUDY",
                [
                    {
                        "formKey": "F_DM",
                        "siteName": "SITE",
                        "data": {"V_AGE": 38, "V_GENDER": "Female"},
                    }
                ],
                schema=strict_dd,
            )
            updater = RecordUpdateWorkflow(sdk, schema=strict_dd)
            batch = updater.create_or_update_records_batch(
                "STUDY",
                [
                    {
                        "formKey": "F_VS",
                        "subjectKey": "SUB1",
                        "intervalName": "Baseline",
                        "data": {"V_WEIGHT": 70.5},
                    }
                ],
            )
            print(
                f"Validated submissions accepted: registration batch={job.batch_id}, "
                f"vitals records={batch.total_records}"
            )

            try:
                sdk.records.create(
                    "STUDY",
                    [
                        {
                            "formKey": "F_DM",
                            "siteName": "SITE",
                            "data": {"V_AGE": 16, "V_GENDER": "Female"},
                        }
                    ],
                    schema=strict_dd,
                )
            except ValidationError as exc:
                print(f"Blocked non-compliant Record locally before API upload ({exc})")


if __name__ == "__main__":
    main()
