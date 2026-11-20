"""Demonstrate DataDictionary-enriched UATWorkflow synthesis and UATExecutionEngine."""

import os

from imednet import ImednetSDK, load_config
from imednet.datadict import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
    parse_logic_xml,
)
from imednet.simulation import SimulationState, SimulationTransport
from imednet_workflows.uat import UATExecutionEngine, UATWorkflow

WEIGHT_RULE_XML = (
    "<rule>"
    '<conditions><condition variable="V_WEIGHT" operator="&lt;" value="35"/></conditions>'
    '<actions><action type="error" message="Baseline weight must be at least 35 kg."/></actions>'
    "</rule>"
)


def build_study_data_dictionary(study_key: str) -> DataDictionary:
    """Build an offline DataDictionary with Choice lists, bounds, and XML edit checks."""
    return DataDictionary(
        metadata=Metadata.create(study_key=study_key),
        forms=[
            Form(
                formKey="F_DM",
                formName="Demographics",
                formType="Enrollment",
                variables=[
                    Variable(
                        variableName="V_AGE",
                        variableType="Integer",
                        required="Yes",
                        extraAttributes={"minValue": 18, "maxValue": 85},
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
            ),
            Form(
                formKey="F_VS",
                formName="Vital Signs",
                formType="Standard",
                variables=[
                    Variable(
                        variableName="V_WEIGHT",
                        variableType="Float",
                        required="Yes",
                        extraAttributes={"minValue": 35.0, "maxValue": 180.0},
                    )
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="101",
                        name="Minimum Weight Edit Check",
                        type="Form",
                        status="Active",
                        form="F_VS",
                        logic=parse_logic_xml(WEIGHT_RULE_XML),
                    )
                ],
            ),
        ],
    )


def _open_sdk() -> tuple[ImednetSDK, str]:
    """Return an SDK client and target Study key using live config or simulation mode."""
    try:
        cfg = load_config()
    except ValueError:
        return (
            ImednetSDK(
                simulation_mode=True,
                transport=SimulationTransport(SimulationState()),
            ),
            "STUDY",
        )

    if cfg.simulation_mode:
        return (
            ImednetSDK(
                simulation_mode=True,
                transport=SimulationTransport(SimulationState()),
            ),
            os.environ.get("IMEDNET_STUDY_KEY", "STUDY"),
        )

    return (
        ImednetSDK(
            api_key=cfg.api_key,
            security_key=cfg.security_key,
            base_url=cfg.base_url,
        ),
        os.environ.get("IMEDNET_STUDY_KEY", "STUDY"),
    )


def main() -> None:
    """Run DataDictionary-enriched UAT synthesis and edit check verification."""
    sdk, study_key = _open_sdk()
    data_dictionary = build_study_data_dictionary(study_key)

    with sdk:
        workflow = UATWorkflow(
            sdk,
            poll_interval=0.0,
            seed=42,
            data_dictionary=data_dictionary,
        )
        run_result = workflow.run(study_key, subject_count=2)
        print(run_result.summary())

        engine = UATExecutionEngine(sdk, data_dictionary, poll_interval=0.0)
        report = engine.run_verification(study_key, form_scope="F_VS")
        print(
            f"Edit check rules evaluated: {report.total_rules} "
            f"(passed={report.passed_rules}, failed={report.failed_rules}, "
            f"skipped={report.skipped_rules})"
        )


if __name__ == "__main__":
    main()
