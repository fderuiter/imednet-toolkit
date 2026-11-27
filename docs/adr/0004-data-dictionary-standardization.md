# Data Dictionary Standardization

## Context and Decision

The iMednet platform provides study metadata across multiple surfaces, primarily through 4-file CSV data dictionary exports (`FORMS.csv`, `QUESTIONS.csv`, `CHOICES.csv`, `BUSINESS_LOGIC.csv`) and through EDC REST API endpoints (`/forms`, `/variables`). Previously, data dictionary handling was minimal, consisting of untyped dictionary rows in `imednet.validation.data_dictionary`, with no unified schema, structural diffing, or typed representations of complex business logic rules.

Furthermore, several inconsistencies existed in nomenclature and structure:
1. **Domain Nomenclature**: Domain terminology in `CONTEXT.md` establishes `Variable` as the canonical term for a CRF data point, yet CSV exports historically labeled the file `QUESTIONS.csv`.
2. **Field Casing**: Headers in raw CSV files use mixed title casing (`Form Key`, `Variable Name`, `Special Patient ID`).
3. **Date Formats**: iMednet CSV exports empirically use `MM-DD-YYYY` timestamp formatting.
4. **Required Field Policy**: `Required Field` in iMednet is not a binary boolean; it carries semantic values such as `Auto Query`.
5. **Business Logic XML Rules**: Rule logic is defined in XML containing empty tag markers (`<true/>`, `<new_record/>`, `<last/>`) and repeated sibling elements representing condition groups and action sets.

We decided to establish a canonical `imednet.datadict` module with the following architecture:
1. **Strict Domain Alignment**: Model child collections on `Form` as `variables: list[Variable]` (deprecating "question" in documentation and domain models), mapping `QUESTIONS.csv` into `variables`.
2. **Lexical camelCasing**: Normalize headers strictly using lexical camelCasing (e.g., `formKey`, `variableName`, `specialPatientId`, `patientRecordReport`) without semantic renames.
3. **Pydantic v2 Canonical Models**: Implement `DataDictionary`, `Form`, `Variable`, `Choice`, `BusinessLogicRule`, and `Metadata` models with JSON Schema emission and deterministic formatting.
4. **Empirical Timestamp Assertion**: Strictly assert and validate `MM-DD-YYYY` timestamps at parse time.
5. **Business Logic XML Parsing**: Transform XML logic into a structured AST dictionary where empty tags become boolean `True` markers, repeated sibling tags collapse into arrays, and outer envelopes unwrap to `(systemGenerated, conditions, actions)`.
6. **Dual Representation and Optional Raw XML**: Support both structured `logic` AST and verbatim `logicRawXml`, allowing `logicRawXml` to be omitted via `--no-raw-xml`. Unknown columns are tracked in `metadata.validationWarnings` and `extraAttributes`.
7. **Rule Routing**: Route `Form`-scoped rules to `form.businessLogic`, and other rule types (`Workflow`, `Query`, `Approval`, `Required`) to top-level `unassignedBusinessLogic`.
8. **Deterministic JSON Emission & Equality**: Sort forms by `formKey`, variables by `sequence` then `variableName`, choices by `position`, and rules by `sequence` then `id`. Exclude `generatedAt` during fixture and model equality comparisons.
9. **Backward-Compatible Shims**: Refactor `imednet.validation.data_dictionary` to re-export `DataDictionary` and provide deprecated `DataDictionaryLoader` methods issuing `FutureWarning`.
10. **Argparse CLI Integration**: Integrate `imednet datadict` subcommands (`from-csv`, `from-api`, `from-odm`, `to-odm`, `validate`, `diff`, `schema`) into the core CLI framework.
11. **Read-Only CDISC Clinical, Administrative, and Audit Exchange Extension**: Extend standardization beyond `MetaDataVersion` metadata to immutable (`frozen=True`, `extra="forbid"`) exchange models in `imednet.datadict.exchange`—re-exported across `imednet.spi.cdisc` and `imednet.spi.models` so workflow and provider extensions never import internal core modules:
    - **CDISC ODM 1.3.2 Archive Models**: `OdmArchive`, `OdmAdminData` (`OdmUser`, `OdmLocation`, `OdmMetaDataVersionRef`), `OdmReferenceData`, `OdmClinicalData` (`OdmSubjectData`, `OdmStudyEventData`, `OdmFormData`, `OdmItemGroupData`, `OdmItemData`), `OdmAuditRecord` (mapped from iMednet `RecordRevision` chains), and `OdmAnnotation` / `OdmFlag` (mapped from iMednet `Query` and `QueryComment` items).
    - **CDISC Dataset-JSON v1.1 Exchange Models**: `DatasetJsonEnvelope`, `DatasetJsonColumn` (`DatasetJsonItem`), `DatasetJsonSourceSystem`, and `DatasetJsonBundle` representing flat single-dataset CDISC Dataset-JSON v1.1 envelopes (`datasetJSONCreationDateTime`, `datasetJSONVersion`, `fileOID`, `sourceSystem`, `studyOID`, `metaDataVersionOID`, `itemGroupOID`, `records`, `name`, `label`, `columns`, `rows`) and typed multi-dataset Python bundles for both CDASH `Form` definitions and normalized SDTM domains without custom vendor JSON hierarchies.
    - **Strict Inbound Safety & Default PHI Redaction**: Enforce `defusedxml` parsing on all ODM XML inputs, fail fast in `load_from_cdisc_odm(..., strict=True)` when non-metadata containers appear while providing `load_odm_archive` for full archive snapshots, and apply `SensitivityRegistry` PHI masking by default across ODM `<ItemData>` values, `<Annotation>` comments, and Dataset-JSON tuples.

## Consequences

- All tools, pipelines, and agents share canonical read-only representations and bundled official schemas (`resources/data-dictionary-1.0.0.schema.json`, `resources/cdisc-odm-1.3.2.xsd`, and `resources/dataset-json-1.1.0.schema.json`) across study metadata, clinical records, 21 CFR Part 11 audit trails, and query annotations.
- Automated diffing, migration testing, regulatory archive exports (`imednet export odm`, `imednet export dataset-json`), and CI validation deterministically exchange CDISC-compliant snapshots without upstream EDC mutations or cross-package boundary violations.
- Full backward compatibility is preserved for existing tests and consumers, including `UATExecutionEngine`.
