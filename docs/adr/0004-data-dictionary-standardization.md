# Data Dictionary Standardization

**Status:** Accepted (temporary compatibility shim in item 9 retired; `imednet.datadict` is canonical)

> **Supersession Note:** `imednet.datadict` remains the canonical Data Dictionary package across the workspace. Item 9 and Consequence 3 below are **superseded**: the temporary backward-compatibility re-export shim in the legacy validation module was removed after workspace migration, so callers must import `DataDictionary`, loaders, diffs, and rule evaluators directly from `imednet.datadict` (and `imednet.spi.validation` for validator protocols). Item 10 has also expanded to include `from-odm`, `to-odm`, and `from-redcap` CLI subcommands alongside `from-csv`, `from-api`, `validate`, `diff`, and `schema` (see [ADR-0007](0007-cli-framework-and-output-contracts.md)).

## Context and Decision

The iMednet platform provides Study metadata across multiple surfaces, primarily through 4-file CSV Data Dictionary exports (`FORMS.csv`, `QUESTIONS.csv`, `CHOICES.csv`, `BUSINESS_LOGIC.csv`) and through EDC REST API endpoints (`/forms`, `/variables`). Previously, Data Dictionary handling was minimal, consisting of untyped dictionary rows in a legacy validation helper (since removed in favor of `imednet.datadict`), with no unified schema, structural diffing, or typed representations of complex business logic rules.

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
9. **Backward-Compatible Shims** *(Superseded)*: Originally provided a temporary deprecated re-export module in `imednet.validation` issuing `FutureWarning` during migration. That transitional shim has been removed; import `DataDictionary` and CSV/API/ODM/REDCap loaders directly from `imednet.datadict`.
10. **Argparse CLI Integration**: Integrate `imednet datadict` subcommands (`from-csv`, `from-api`, `from-odm`, `to-odm`, `from-redcap`, `validate`, `diff`, `schema`) into the core `argparse` CLI framework.

## Consequences

- All tools, pipelines, and agents share a single canonical JSON representation and JSON Schema (`resources/data-dictionary-1.0.0.schema.json`) for clinical Study metadata.
- Automated diffing, migration testing, and CI validation can deterministically detect changes in Study configurations across environments.
- *(Superseded)* The temporary transition shim for legacy callers was retired once all workspace consumers, including `UATExecutionEngine`, migrated to `imednet.datadict`.
