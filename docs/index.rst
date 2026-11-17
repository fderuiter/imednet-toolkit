.. note::
   This SDK is unofficial and not affiliated with the vendor. Use at your own risk.

Welcome to imednet's documentation!
===================================

.. _interactive-showcase:

Role-Based Pathways & Interactive Showcase
------------------------------------------

.. container:: role-cards-grid

   .. container:: role-card role-pathway-section

      .. rubric:: Clinical Data Managers
         :class: role-card-title

      Curated workflows for clinical study configuration, subject registration, form building, and data verification.

      .. raw:: html

         <div class="role-card-action">
            <button type="button" class="role-card-launch-btn" data-persona-target="panel-datamanager" aria-label="Inspect Form Schemas - Open Clinical Data Manager Live Explorer">
               <span>📋 Inspect Form Schemas</span>
            </button>
         </div>

      * :doc:`how-to/register_subjects`
      * :doc:`how-to/form_builder`
      * :doc:`how-to/workflows/bulk_submission`
      * :doc:`how-to/verification`

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

      .. rubric:: Regulatory Compliance Auditors
         :class: role-card-title

      Accessibility conformance disclosures (VPAT / WCAG 2.1 AA), governance policies, standards, and architecture records.

      .. raw:: html

         <div class="role-card-action">
            <button type="button" class="role-card-launch-btn" data-persona-target="panel-auditor" aria-label="Verify Audit Trails - Open Regulatory Auditor Live Verification">
               <span>🛡️ Verify Audit Trails</span>
            </button>
         </div>

      * :doc:`reference/VPAT`
      * :doc:`reference/governance`
      * :doc:`reference/project_standards`
      * :doc:`reference/security_exceptions`
      * :doc:`adr/index`

.. raw:: html

   <div id="interactive-showcase-root" class="interactive-showcase-container">
      <div class="showcase-header">
         <div class="showcase-header-content">
            <div class="showcase-header-text">
               <h3>Embedded Live Clinical Explorer</h3>
               <p>Execute client-side Python SDK calls, REST queries, and form schema validations directly in the browser.</p>
            </div>
            <div class="showcase-client-badge">
               <span>⚡ 100% Client-Side (Sub-50ms)</span>
            </div>
         </div>
         <div class="showcase-persona-tabs" role="tablist" aria-label="Live Explorer Persona Selection">
            <button type="button" class="showcase-persona-tab active" id="tab-integration" role="tab" aria-selected="true" aria-controls="panel-integration" data-target="panel-integration">
               Integration Engineer
            </button>
            <button type="button" class="showcase-persona-tab" id="tab-datamanager" role="tab" aria-selected="false" aria-controls="panel-datamanager" data-target="panel-datamanager">
               Clinical Data Manager
            </button>
            <button type="button" class="showcase-persona-tab" id="tab-auditor" role="tab" aria-selected="false" aria-controls="panel-auditor" data-target="panel-auditor">
               Regulatory Compliance Auditor
            </button>
         </div>
      </div>

      <!-- Panel 1: Integration Engineer -->
      <div id="panel-integration" class="showcase-panel active" role="tabpanel" tabindex="0" aria-labelledby="tab-integration">
         <div class="explorer-toolbar">
            <div class="explorer-controls-left">
               <div class="mode-segmented-toggle" role="group" aria-label="Execution Protocol">
                  <button type="button" id="ie-mode-sdk" class="mode-pill-btn active" aria-pressed="true">Python SDK</button>
                  <button type="button" id="ie-mode-rest" class="mode-pill-btn" aria-pressed="false">REST Request</button>
               </div>
               <div class="explorer-control-group">
                  <label for="ie-dataset-select" class="explorer-control-label">Sample Query:</label>
                  <select id="ie-dataset-select" class="explorer-select" aria-label="Select sample dataset query">
                     <option value="records">Clinical Records (Vital Signs - VS)</option>
                     <option value="studies">Active Clinical Trials Catalog</option>
                     <option value="forms">Case Report Form Schemas</option>
                     <option value="queries">Clinical Queries &amp; Discrepancies</option>
                  </select>
               </div>
            </div>
            <div class="explorer-controls-right">
               <button type="button" id="ie-run-btn" class="explorer-run-btn" aria-label="Execute query in browser">
                  <span>▶ Execute Query</span>
               </button>
            </div>
         </div>

         <div class="explorer-editor-box">
            <div class="explorer-editor-header">
               <span>Client-Side Query Editor</span>
               <span>In-memory sandbox</span>
            </div>
            <textarea id="ie-code-input" class="explorer-textarea" rows="6" aria-label="Code editor" spellcheck="false"></textarea>
         </div>

         <div class="explorer-metric-bar">
            <div id="ie-metric-badge" class="explorer-metric-badge">
               ⚡ Executed in 1.8 ms (Mock Client Engine) | Status: 200 OK
            </div>
            <div class="explorer-output-tabs" role="tablist" aria-label="Output View">
               <button type="button" id="ie-tab-table" class="explorer-output-tab active" role="tab" aria-selected="true" aria-controls="ie-view-table">
                  Tabular View (DataFrame)
               </button>
               <button type="button" id="ie-tab-json" class="explorer-output-tab" role="tab" aria-selected="false" aria-controls="ie-view-json">
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
            <p>Ready to integrate with live clinical databases and pipeline orchestrators?</p>
            <a href="tutorials/quick_start.html">Explore the Quick Start Tutorial &rarr;</a>
         </div>
      </div>

      <!-- Panel 2: Clinical Data Manager -->
      <div id="panel-datamanager" class="showcase-panel" role="tabpanel" tabindex="0" aria-labelledby="tab-datamanager" hidden>
         <div class="explorer-toolbar">
            <div class="explorer-controls-left">
               <div class="explorer-control-group">
                  <label for="cdm-form-select" class="explorer-control-label">Study Form:</label>
                  <select id="cdm-form-select" class="explorer-select" aria-label="Select study form to inspect">
                     <option value="VS">VS — Vital Signs (Blood pressure, pulse, temperature)</option>
                     <option value="DM">DM — Demographics (Age, sex, race, treatment arm)</option>
                     <option value="AE">AE — Adverse Events (Term, severity, causality)</option>
                     <option value="LB">LB — Laboratory Findings (Analytes, units, ranges)</option>
                  </select>
               </div>
            </div>
            <div class="explorer-controls-right">
               <button type="button" id="cdm-validate-btn" class="explorer-run-btn" aria-label="Validate and render form schema">
                  <span>✓ Validate &amp; Render Form</span>
               </button>
            </div>
         </div>

         <div class="explorer-metric-bar">
            <div id="cdm-metric-badge" class="explorer-metric-badge">
               ✓ Schema Valid (CDISC ODM Compliant) — 0 Errors | Validated in 0.9 ms
            </div>
            <div class="explorer-output-tabs" role="tablist" aria-label="Form Schema View">
               <button type="button" id="cdm-tab-crf" class="explorer-output-tab active" role="tab" aria-selected="true" aria-controls="cdm-view-crf">
                  CRF Structure
               </button>
               <button type="button" id="cdm-tab-json" class="explorer-output-tab" role="tab" aria-selected="false" aria-controls="cdm-view-json">
                  Form Schema JSON
               </button>
            </div>
         </div>

         <div id="cdm-view-crf" class="explorer-output-view" role="tabpanel" tabindex="0" aria-labelledby="cdm-tab-crf"></div>

         <div id="cdm-view-json" class="explorer-output-view explorer-json-wrapper" role="tabpanel" tabindex="0" aria-labelledby="cdm-tab-json" hidden>
            <button type="button" id="cdm-copy-btn" class="explorer-copy-btn" aria-label="Copy schema JSON">Copy JSON</button>
            <pre id="cdm-json-pre" class="explorer-json-pre"><code class="language-json"></code></pre>
         </div>

         <div class="quick-start-callout">
            <p>Design complex clinical trials with dynamic conditional logic and edit checks:</p>
            <a href="how-to/form_builder.html">Visit Form Builder Guide &rarr;</a>
         </div>
      </div>

      <!-- Panel 3: Regulatory Compliance Auditor -->
      <div id="panel-auditor" class="showcase-panel" role="tabpanel" tabindex="0" aria-labelledby="tab-auditor" hidden>
         <div class="explorer-toolbar">
            <div class="explorer-controls-left">
               <div class="explorer-control-group">
                  <label for="aud-scenario-select" class="explorer-control-label">Audit Scope:</label>
                  <select id="aud-scenario-select" class="explorer-select" aria-label="Select audit scope">
                     <option value="part11">21 CFR Part 11 Electronic Signature &amp; Audit Trail</option>
                     <option value="vpat">WCAG 2.1 AA Accessibility Conformance Verification</option>
                  </select>
               </div>
            </div>
            <div class="explorer-controls-right">
               <button type="button" id="aud-verify-btn" class="explorer-run-btn" aria-label="Verify audit trail and accessibility compliance">
                  <span>🛡️ Verify Audit Integrity</span>
               </button>
            </div>
         </div>

         <div class="explorer-metric-bar">
            <div id="aud-metric-badge" class="explorer-metric-badge">
               ✓ 21 CFR Part 11 Electronic Signature &amp; VPAT Check Verified | 1.1 ms
            </div>
            <div class="explorer-output-tabs" role="tablist" aria-label="Auditor View">
               <button type="button" id="aud-tab-table" class="explorer-output-tab active" role="tab" aria-selected="true" aria-controls="aud-view-table">
                  Audit Trail Records
               </button>
               <button type="button" id="aud-tab-json" class="explorer-output-tab" role="tab" aria-selected="false" aria-controls="aud-view-json">
                  Signed Event JSON
               </button>
            </div>
         </div>

         <div id="aud-view-table" class="explorer-output-view" role="tabpanel" tabindex="0" aria-labelledby="aud-tab-table"></div>

         <div id="aud-view-json" class="explorer-output-view explorer-json-wrapper" role="tabpanel" tabindex="0" aria-labelledby="aud-tab-json" hidden>
            <button type="button" id="aud-copy-btn" class="explorer-copy-btn" aria-label="Copy audit JSON">Copy JSON</button>
            <pre id="aud-json-pre" class="explorer-json-pre"><code class="language-json"></code></pre>
         </div>

         <div class="quick-start-callout">
            <p>Review our formal accessibility conformance disclosures and compliance records:</p>
            <a href="reference/VPAT.html">Read VPAT Statement &rarr;</a>
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
   how-to/logging_and_tracing
   how-to/pagination
   how-to/form_builder
   how-to/register_subjects
   how-to/caching
   how-to/retry_policy
   how-to/job_polling
   how-to/airflow
   how-to/workflows/uat_workflow
   how-to/workflows/bulk_submission
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
   explanation/refactoring_patterns

.. toctree::
   :maxdepth: 2
   :caption: Reference:

   reference/rest_api_reference
   reference/configuration
   reference/compatibility
   reference/agent_rules
   reference/refactoring_roadmaps
   reference/test_skip_conditions
   reference/live_test_charter
   reference/live_test_plan
   reference/live_tests
   reference/contributing
   reference/issue_management
   reference/project_standards
   reference/triage_playbook
   reference/governance
   reference/verification_reports
   reference/security_exceptions
   reference/VPAT
   adr/index
   reference/api/core
   reference/api/plugins-sinks
   reference/api/plugins-tui
   reference/api/plugins-workflows
   reference/api/providers-airflow

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
