.. meta::
   :description: Unofficial Python SDK, CLI, workflow plugins, TUI, and Apache Airflow provider for the iMednet clinical study EDC API.

.. note::
   This SDK is unofficial and not affiliated with the vendor. Use at your own risk.

Welcome to imednet's documentation!
===================================

.. _interactive-showcase:

Role-Based Pathways & Interactive Showcase
------------------------------------------

.. container:: role-cards-grid

   .. container:: role-card role-pathway-section

      .. rubric:: Integration Engineers
         :class: role-card-title

      Quick-start tutorials, CLI tooling, Airflow orchestration, and core Python SDK and REST API references.

      .. raw:: html

         <div class="role-card-action">
            <button type="button" class="role-card-launch-btn" data-persona-target="panel-integration" aria-label="Run Interactive Queries - Open Integration Engineer Live Console">
               <span>⚡ Run Interactive Queries</span>
            </button>
         </div>

      * :doc:`tutorials/quick_start`
      * :doc:`how-to/cli`
      * :doc:`how-to/airflow`
      * :doc:`reference/rest_api_reference`
      * :doc:`reference/api/core`

   .. container:: role-card role-pathway-section

      .. rubric:: Clinical Data Managers
         :class: role-card-title

      Curated workflows for Study Data Dictionary schemas, pre-live UAT validation, Subject registration, and bulk Record creation using ``imednet datadict`` (``from-csv``, ``from-odm``, ``to-odm``, ``from-redcap``, ``from-api``, ``validate``, ``diff``, and ``schema``).

      .. raw:: html

         <div class="role-card-action">
            <button type="button" class="role-card-launch-btn" data-persona-target="panel-datamanager" aria-label="Inspect Form Schemas - Open Clinical Data Manager Live Explorer">
               <span>📋 Inspect Form Schemas</span>
            </button>
         </div>

      * :doc:`how-to/data_dictionary`
      * :doc:`how-to/workflows/uat_workflow`
      * :doc:`how-to/register_subjects`
      * :doc:`how-to/workflows/bulk_submission`

   .. container:: role-card role-pathway-section

      .. rubric:: Regulatory Compliance Auditors
         :class: role-card-title

      21 CFR Part 11 Record revision audit trails, analytical DuckDB lineage, PHI Variable masking, configuration governance, and VPAT / WCAG 2.1 AA conformance disclosures.

      .. raw:: html

         <div class="role-card-action">
            <button type="button" class="role-card-launch-btn" data-persona-target="panel-auditor" aria-label="Verify Audit Trails - Open Regulatory Auditor Live Verification">
               <span>🛡️ Verify Audit Trails</span>
            </button>
         </div>

      * :doc:`how-to/audit_compliance`
      * :doc:`how-to/duckdb_integration`
      * :doc:`how-to/export_destinations`
      * :doc:`reference/governance`
      * :doc:`reference/VPAT`

.. raw:: html

   <div id="interactive-showcase-root" class="interactive-showcase-container">
      <div class="showcase-header">
         <div class="showcase-header-content">
            <div class="showcase-header-text">
               <h3>Simulation Scenario Explorer</h3>
               <p>Inspect verified Python SDK and REST snippets alongside instant simulation previews for Study, Form, Variable, Record, and Query resources.</p>
            </div>
            <div class="showcase-client-badge">
               <span>Instant Simulation Preview (Offline Fixtures)</span>
            </div>
         </div>
         <div class="showcase-persona-tabs" role="tablist" aria-label="Live Explorer Persona Selection">
            <button type="button" class="showcase-persona-tab active" id="tab-integration" role="tab" aria-selected="true" tabindex="0" aria-controls="panel-integration" data-target="panel-integration">
               Integration Engineer
            </button>
            <button type="button" class="showcase-persona-tab" id="tab-datamanager" role="tab" aria-selected="false" tabindex="-1" aria-controls="panel-datamanager" data-target="panel-datamanager">
               Clinical Data Manager
            </button>
            <button type="button" class="showcase-persona-tab" id="tab-auditor" role="tab" aria-selected="false" tabindex="-1" aria-controls="panel-auditor" data-target="panel-auditor">
               Regulatory Compliance Auditor
            </button>
         </div>
      </div>

      <!-- Panel 1: Integration Engineer -->
      <div id="panel-integration" class="showcase-panel active" role="tabpanel" tabindex="0" aria-labelledby="tab-integration">
         <div class="explorer-toolbar">
            <div class="explorer-controls-left">
               <div class="mode-segmented-toggle" role="group" aria-label="Snippet Format">
                  <button type="button" id="ie-mode-sdk" class="mode-pill-btn active" aria-pressed="true">Python SDK</button>
                  <button type="button" id="ie-mode-rest" class="mode-pill-btn" aria-pressed="false">REST Request</button>
               </div>
               <div class="explorer-control-group">
                  <label for="ie-dataset-select" class="explorer-control-label">Study Scenario:</label>
                  <select id="ie-dataset-select" class="explorer-select" aria-label="Study Scenario: Select simulation preview scenario">
                     <option value="records">Study Records (Vital Signs — F_VS)</option>
                     <option value="studies">Clinical Studies Catalog</option>
                     <option value="forms">Study Form Schemas</option>
                     <option value="queries">Open Study Queries</option>
                  </select>
               </div>
            </div>
         </div>

         <div class="explorer-editor-box">
            <div class="explorer-editor-header">
               <span>Read-Only Scenario Snippet (STUDY)</span>
               <button type="button" id="ie-snippet-copy-btn" class="explorer-snippet-copy-btn" aria-label="Copy Code — Copy scenario snippet">Copy Code</button>
            </div>
            <pre id="ie-code-viewer" class="explorer-code-pre"><code id="ie-code-display" class="language-python"></code></pre>
         </div>

         <div class="explorer-metric-bar">
            <div id="ie-metric-badge" class="explorer-metric-badge" role="status" aria-live="polite" aria-atomic="true">
               Simulation Preview (Deterministic Fixture) — STUDY Records (4 items)
            </div>
            <div class="explorer-output-tabs" role="tablist" aria-label="Output View">
               <button type="button" id="ie-tab-table" class="explorer-output-tab active" role="tab" aria-selected="true" tabindex="0" aria-controls="ie-view-table">
                  Tabular View (DataFrame)
               </button>
               <button type="button" id="ie-tab-json" class="explorer-output-tab" role="tab" aria-selected="false" tabindex="-1" aria-controls="ie-view-json">
                  Raw JSON Payload
               </button>
            </div>
         </div>

         <div id="ie-view-table" class="explorer-output-view" role="tabpanel" tabindex="0" aria-labelledby="ie-tab-table"></div>

         <div id="ie-view-json" class="explorer-output-view explorer-json-wrapper" role="tabpanel" tabindex="0" aria-labelledby="ie-tab-json" hidden>
            <button type="button" id="ie-copy-btn" class="explorer-copy-btn" aria-label="Copy JSON payload">Copy JSON</button>
            <pre id="ie-json-pre" class="explorer-json-pre"><code class="language-json"></code></pre>
         </div>

         <div class="quick-start-callout">
            <p>Want to edit and execute custom Python <code>ImednetSDK</code> calls interactively in your browser?</p>
            <a href="tutorials/quick_start.html">Launch the Interactive Runtime in Quick Start &rarr;</a>
         </div>
      </div>

      <!-- Panel 2: Clinical Data Manager -->
      <div id="panel-datamanager" class="showcase-panel" role="tabpanel" tabindex="0" aria-labelledby="tab-datamanager" hidden>
         <div class="explorer-toolbar">
            <div class="explorer-controls-left">
               <div class="explorer-control-group">
                  <label for="cdm-form-select" class="explorer-control-label">Study Form:</label>
                  <select id="cdm-form-select" class="explorer-select" aria-label="Study Form: Select study form schema to inspect">
                     <option value="VS">F_VS — Vital Signs (Blood pressure, pulse, weight, temperature)</option>
                     <option value="DM">F_DM — Demographics (Age, sex, race, treatment arm)</option>
                     <option value="AE">F_AE — Safety Observations (Term, severity, causality)</option>
                     <option value="LB">F_LB — Laboratory Findings (Analytes, units, ranges)</option>
                  </select>
               </div>
            </div>
            <div class="explorer-controls-right">
               <button type="button" id="cdm-validate-btn" class="explorer-run-btn" aria-label="Validate &amp; Render Form — Validate and render form schema">
                  <span>✓ Validate &amp; Render Form</span>
               </button>
            </div>
         </div>

         <div class="explorer-metric-bar">
            <div id="cdm-metric-badge" class="explorer-metric-badge" role="status" aria-live="polite" aria-atomic="true">
               Simulation Preview (Deterministic Fixture) — Form Schema Valid (CDISC ODM Compliant, 0 Errors)
            </div>
            <div class="explorer-output-tabs" role="tablist" aria-label="Form Schema View">
               <button type="button" id="cdm-tab-crf" class="explorer-output-tab active" role="tab" aria-selected="true" tabindex="0" aria-controls="cdm-view-crf">
                  Form Variables
               </button>
               <button type="button" id="cdm-tab-json" class="explorer-output-tab" role="tab" aria-selected="false" tabindex="-1" aria-controls="cdm-view-json">
                  Form Schema JSON
               </button>
            </div>
         </div>

         <div id="cdm-view-crf" class="explorer-output-view" role="tabpanel" tabindex="0" aria-labelledby="cdm-tab-crf"></div>

         <div id="cdm-view-json" class="explorer-output-view explorer-json-wrapper" role="tabpanel" tabindex="0" aria-labelledby="cdm-tab-json" hidden>
            <button type="button" id="cdm-copy-btn" class="explorer-copy-btn" aria-label="Copy JSON — Copy schema JSON">Copy JSON</button>
            <pre id="cdm-json-pre" class="explorer-json-pre"><code class="language-json"></code></pre>
         </div>

         <div class="quick-start-callout">
            <p>Compile, convert, diff, and validate canonical Study Data Dictionary schemas with <code>imednet datadict</code> (<code>from-csv</code>, <code>from-odm</code>, <code>to-odm</code>, <code>from-redcap</code>, <code>from-api</code>, <code>validate</code>, <code>diff</code>, <code>schema</code>):</p>
            <div class="quick-start-callout-links">
               <a href="how-to/data_dictionary.html">Data Dictionary Guide &rarr;</a>
               <a href="how-to/workflows/uat_workflow.html">UAT Workflow &rarr;</a>
               <a href="how-to/register_subjects.html">Subject Registration &rarr;</a>
               <a href="how-to/workflows/bulk_submission.html">Bulk Record Creation &rarr;</a>
            </div>
         </div>
      </div>

      <!-- Panel 3: Regulatory Compliance Auditor -->
      <div id="panel-auditor" class="showcase-panel" role="tabpanel" tabindex="0" aria-labelledby="tab-auditor" hidden>
         <div class="explorer-toolbar">
            <div class="explorer-controls-left">
               <div class="explorer-control-group">
                  <label for="aud-scenario-select" class="explorer-control-label">Audit Scope:</label>
                  <select id="aud-scenario-select" class="explorer-select" aria-label="Audit Scope: Select audit scope">
                     <option value="part11">21 CFR Part 11 Electronic Signature &amp; Audit Trail</option>
                     <option value="vpat">WCAG 2.1 AA Accessibility Conformance Verification</option>
                  </select>
               </div>
            </div>
            <div class="explorer-controls-right">
               <button type="button" id="aud-verify-btn" class="explorer-run-btn" aria-label="Verify Audit Integrity — Verify audit trail and accessibility compliance">
                  <span>🛡️ Verify Audit Integrity</span>
               </button>
            </div>
         </div>

         <div class="explorer-metric-bar">
            <div id="aud-metric-badge" class="explorer-metric-badge" role="status" aria-live="polite" aria-atomic="true">
               Simulation Preview (Deterministic Fixture) — 21 CFR Part 11 Electronic Signature &amp; VPAT Verified
            </div>
            <div class="explorer-output-tabs" role="tablist" aria-label="Auditor View">
               <button type="button" id="aud-tab-table" class="explorer-output-tab active" role="tab" aria-selected="true" tabindex="0" aria-controls="aud-view-table">
                  Audit Trail Records
               </button>
               <button type="button" id="aud-tab-json" class="explorer-output-tab" role="tab" aria-selected="false" tabindex="-1" aria-controls="aud-view-json">
                  Signed Audit JSON
               </button>
            </div>
         </div>

         <div id="aud-view-table" class="explorer-output-view" role="tabpanel" tabindex="0" aria-labelledby="aud-tab-table"></div>

         <div id="aud-view-json" class="explorer-output-view explorer-json-wrapper" role="tabpanel" tabindex="0" aria-labelledby="aud-tab-json" hidden>
            <button type="button" id="aud-copy-btn" class="explorer-copy-btn" aria-label="Copy JSON — Copy audit JSON">Copy JSON</button>
            <pre id="aud-json-pre" class="explorer-json-pre"><code class="language-json"></code></pre>
         </div>

         <div class="quick-start-callout">
            <p>Verify 21 CFR Part 11 RecordRevision chains, DuckDB lineage, PHI Variable masking, governance provenance, and VPAT disclosures:</p>
            <div class="quick-start-callout-links">
               <a href="how-to/audit_compliance.html">Audit &amp; Compliance Guide &rarr;</a>
               <a href="how-to/duckdb_integration.html">DuckDB Lineage &rarr;</a>
               <a href="how-to/export_destinations.html">PHI Masking &amp; Exports &rarr;</a>
               <a href="reference/governance.html">Governance Provenance &rarr;</a>
               <a href="reference/VPAT.html">Read VPAT Statement &rarr;</a>
            </div>
         </div>
      </div>
   </div>


Documentation Overview
----------------------

This documentation is organized according to the Diátaxis framework into four main sections.

.. toctree::
   :maxdepth: 2
   :caption: Tutorials:

   tutorials/quick_start
   tutorials/async_quick_start
   tutorials/examples/index

.. toctree::
   :maxdepth: 2
   :caption: How-to Guides:

   how-to/cli
   how-to/data_dictionary
   how-to/audit_compliance
   how-to/logging_and_tracing
   how-to/pagination
   how-to/form_builder
   how-to/register_subjects
   how-to/caching
   how-to/retry_policy
   how-to/job_polling
   how-to/domain_lifecycles
   how-to/simulation_testing
   how-to/airflow
   how-to/workflows/uat_workflow
   how-to/workflows/uat_inspector
   how-to/workflows/bulk_submission
   how-to/workflows/data_extraction
   how-to/workflows/record_update
   how-to/workflows/subject_data
   how-to/workflows/query_management
   how-to/workflows/triage_store
   how-to/workflows/schema_profiler
   how-to/workflows/chunked_pipeline
   how-to/export_destinations
   how-to/duckdb_integration
   how-to/snowflake_export
   how-to/orchestration
   how-to/record_mapping
   how-to/verification

.. toctree::
   :maxdepth: 2
   :caption: Explanation:

   explanation/architecture
   explanation/api_overview
   explanation/parsing
   explanation/schema_validation
   explanation/plugins
   explanation/workflow_interactions

.. toctree::
   :maxdepth: 2
   :caption: Reference:

   reference/rest_api_reference
   reference/configuration
   reference/compatibility
   reference/changelogs/index
   reference/governance
   reference/VPAT
   adr/index
   reference/api/core
   reference/api/plugins-sinks
   reference/api/plugins-tui
   reference/api/plugins-workflows
   reference/api/providers-airflow

.. toctree::
   :maxdepth: 2
   :caption: Maintainer & Contributor:

   reference/contributing
   reference/project_standards
   reference/issue_management
   reference/triage_playbook
   reference/agent_rules
   explanation/refactoring_patterns
   reference/refactoring_roadmaps
   reference/test_skip_conditions
   reference/live_test_charter
   reference/live_test_plan
   reference/live_tests
   reference/verification_reports
   reference/security_exceptions

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
