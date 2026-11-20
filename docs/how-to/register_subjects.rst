Register Subjects Workflow
==========================

The ``RegisterSubjectsWorkflow`` validates that site names exist before submitting registration records.
The API assigns the subject identifier upon creation; do not include a ``subjectKey`` in the request.

Example
-------

.. testcode::

    from imednet import ImednetSDK
    from imednet.models.records import RegisterSubjectRequest, RecordData
    from imednet_workflows.register_subjects import RegisterSubjectsWorkflow
    from typing import Any, cast

    sdk = ImednetSDK(api_key="API_KEY", security_key="SECURITY_KEY")
    workflow = RegisterSubjectsWorkflow(cast(Any, sdk))
    subjects = [RegisterSubjectRequest(formKey="REG", siteName="SITE", data=RecordData({}))]
    job = workflow.register_subjects(study_key="STUDY", subjects=subjects)
    print(job)

Validating Enrollment Data with a Data Dictionary
-------------------------------------------------

Both :meth:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow.register_subjects`
and :meth:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow.register_subjects_batch`
accept ``schema=data_dictionary`` (any
:class:`~imednet.validation.cache.ValidationSchemaProtocol`, such as a
:class:`~imednet.datadict.models.DataDictionary`). Passing a ``DataDictionary``
validates every enrollment Record against required Variable policies, Choice
codelists, numeric/text bounds, and XML business logic rules *after* Site
eligibility checks and *before* submitting chunked registration requests:

.. testcode::

    from imednet import ImednetSDK
    from imednet.datadict import Choice, DataDictionary, Form, Metadata, Variable
    from imednet.errors import ValidationError
    from imednet.models.records import RecordData, RegisterSubjectRequest
    from imednet.simulation import SimulationState, SimulationTransport
    from imednet_workflows.register_subjects import RegisterSubjectsWorkflow

    enrollment_dd = DataDictionary(
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
            )
        ],
    )

    with ImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(SimulationState()),
    ) as sim_sdk:
        reg_workflow = RegisterSubjectsWorkflow(sim_sdk, filter_enrolled=True)
        summary = reg_workflow.register_subjects_batch(
            study_key="STUDY",
            subjects=[
                RegisterSubjectRequest(
                    formKey="F_DM",
                    siteName="SITE",
                    data=RecordData({"V_AGE": 42, "V_GENDER": "Female"}),
                )
            ],
            schema=enrollment_dd,
        )
        assert summary.total_records == 1

        try:
            reg_workflow.register_subjects_batch(
                study_key="STUDY",
                subjects=[
                    RegisterSubjectRequest(
                        formKey="F_DM",
                        siteName="SITE",
                        data=RecordData({"V_AGE": 15, "V_GENDER": "Unknown"}),
                    )
                ],
                schema=enrollment_dd,
            )
        except ValidationError as exc:
            assert "V_AGE" in str(exc) or "V_GENDER" in str(exc)

See :doc:`/how-to/data_dictionary` and :doc:`/explanation/schema_validation` for
details on loading Study Data Dictionaries and XML edit checks.
