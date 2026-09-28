# iMednet EDC

Python SDK and developer toolkit for the iMednet clinical trials Electronic Data Capture (EDC) REST API.

## Language

**Study**:
A clinical trial protocol or investigation container managing clinical research data.
_Avoid_: Trial, project, experiment

**Subject**:
A clinical study participant whose clinical data is recorded in the study.
_Avoid_: Patient, participant, candidate

**Site**:
A clinical investigation site (hospital, clinic, or medical center) where subjects are enrolled and evaluated.
_Avoid_: Center, clinic, location

**Form**:
An electronic Case Report Form (eCRF) defining data fields and rules for clinical data entry.
_Avoid_: eCRF, questionnaire, survey

**Record**:
A submitted or draft instance of form data for a specific subject, site, and visit.
_Avoid_: Submission, entry, row

**Visit**:
A clinical encounter or evaluation event (scheduled or unscheduled) in a study timeline.
_Avoid_: Appointment, event, milestone

**Interval**:
A defined timeframe or scheduled phase grouping visits across a study protocol.
_Avoid_: Phase, window, period

**Variable**:
An individual clinical data point or field defined within a form.
_Avoid_: Field, column, parameter, question

**Query**:
A formal data discrepancy flag or clarification request raised against a record or variable.
_Avoid_: Discrepancy, ticket, flag

**Data Dictionary**:
A standardized specification of study metadata defining forms, variables, choices, and business logic rules.
_Avoid_: Codebook, schema definition, CRF specification

**Job**:
An asynchronous background task executed by the iMednet EDC API, such as a bulk record import or export.
_Avoid_: Task, process, worker
