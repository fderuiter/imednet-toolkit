/**
 * Interactive Showcase & Simulation Scenario Explorer for iMednet Documentation Portal
 * Deterministic simulation preview viewer with preloaded Study, Form, Variable,
 * Record, and Query fixtures, persona tabs, tabular DataFrame rendering,
 * raw JSON viewer, and WCAG 2.1 AA accessibility.
 */

(function () {
    "use strict";

    // -------------------------------------------------------------------------
    // Preloaded Deterministic Simulation Datasets (De-identified synthetic data; Zero PHI)
    // -------------------------------------------------------------------------
    const MOCK_DATA = {
        studies: [
            {
                sponsorKey: "ACME-SPONSOR",
                studyKey: "STUDY",
                studyId: 1003,
                studyName: "Benchmark Validation Study",
                studyDescription: "Benchmark simulated study",
                studyType: "Observational",
                dateCreated: "2026-01-01T00:00:00Z",
                dateModified: "2026-01-15T12:00:00Z",
                lastUpdated: "2026-01-15T12:00:00Z"
            },
            {
                sponsorKey: "ACME-SPONSOR",
                studyKey: "DEMO-STUDY",
                studyId: 1001,
                studyName: "Demo Oncology Study",
                studyDescription: "Simulated oncology biomarker study",
                studyType: "Interventional",
                dateCreated: "2026-01-01T00:00:00Z",
                dateModified: "2026-01-15T12:00:00Z",
                lastUpdated: "2026-01-15T12:00:00Z"
            },
            {
                sponsorKey: "ACME-SPONSOR",
                studyKey: "MY_STUDY",
                studyId: 1002,
                studyName: "Primary Clinical Study",
                studyDescription: "Primary simulated clinical investigation",
                studyType: "Interventional",
                dateCreated: "2026-01-01T00:00:00Z",
                dateModified: "2026-01-15T12:00:00Z",
                lastUpdated: "2026-01-15T12:00:00Z"
            }
        ],
        forms: {
            VS: {
                studyKey: "STUDY",
                formId: 302,
                formKey: "F_VS",
                formName: "Vital Signs",
                formType: "CRF",
                revision: 1,
                dateCreated: "2026-01-01T00:00:00Z",
                dateModified: "2026-01-15T12:00:00Z",
                disabled: false,
                eproForm: false,
                allowCopy: false,
                embeddedLog: false,
                enforceOwnership: false,
                otherForms: false,
                userAgreement: false,
                lastUpdated: "2026-01-15T12:00:00Z",
                subjectRecordReport: false,
                unscheduledVisit: false
            },
            DM: {
                studyKey: "STUDY",
                formId: 301,
                formKey: "F_DM",
                formName: "Demographics",
                formType: "CRF",
                revision: 1,
                dateCreated: "2026-01-01T00:00:00Z",
                dateModified: "2026-01-15T12:00:00Z",
                disabled: false,
                eproForm: false,
                allowCopy: false,
                embeddedLog: false,
                enforceOwnership: false,
                otherForms: false,
                userAgreement: false,
                lastUpdated: "2026-01-15T12:00:00Z",
                subjectRecordReport: true,
                unscheduledVisit: false
            },
            AE: {
                studyKey: "STUDY",
                formId: 303,
                formKey: "F_AE",
                formName: "Safety Observations",
                formType: "CRF",
                revision: 1,
                dateCreated: "2026-01-01T00:00:00Z",
                dateModified: "2026-01-15T12:00:00Z",
                disabled: false,
                eproForm: false,
                allowCopy: true,
                embeddedLog: false,
                enforceOwnership: false,
                otherForms: false,
                userAgreement: false,
                lastUpdated: "2026-01-15T12:00:00Z",
                subjectRecordReport: false,
                unscheduledVisit: true
            },
            LB: {
                studyKey: "STUDY",
                formId: 304,
                formKey: "F_LB",
                formName: "Laboratory Findings",
                formType: "CRF",
                revision: 1,
                dateCreated: "2026-01-01T00:00:00Z",
                dateModified: "2026-01-15T12:00:00Z",
                disabled: false,
                eproForm: false,
                allowCopy: false,
                embeddedLog: false,
                enforceOwnership: false,
                otherForms: false,
                userAgreement: false,
                lastUpdated: "2026-01-15T12:00:00Z",
                subjectRecordReport: false,
                unscheduledVisit: false
            }
        },
        records: {
            VS: [
                { recordId: 601, studyKey: "STUDY", subjectKey: "SUB1", formKey: "F_VS", visitId: 401, V_SYSBP: 122, V_DIABP: 78, V_PULSE: 68, V_WEIGHT: 68.5, recordStatus: "Complete" },
                { recordId: 602, studyKey: "STUDY", subjectKey: "SUB2", formKey: "F_VS", visitId: 401, V_SYSBP: 138, V_DIABP: 86, V_PULSE: 74, V_WEIGHT: 74.2, recordStatus: "Complete" },
                { recordId: 603, studyKey: "STUDY", subjectKey: "SUB3", formKey: "F_VS", visitId: 402, V_SYSBP: 118, V_DIABP: 74, V_PULSE: 62, V_WEIGHT: 64.0, recordStatus: "Verified" },
                { recordId: 604, studyKey: "STUDY", subjectKey: "SUB4", formKey: "F_VS", visitId: 401, V_SYSBP: 144, V_DIABP: 92, V_PULSE: 82, V_WEIGHT: 81.3, recordStatus: "Complete" }
            ],
            DM: [
                { recordId: 611, studyKey: "STUDY", subjectKey: "SUB1", formKey: "F_DM", V_AGE: 45, V_GENDER: "Female", V_RACE: "WHITE", V_ARM: "ARM-A", recordStatus: "Locked" },
                { recordId: 612, studyKey: "STUDY", subjectKey: "SUB2", formKey: "F_DM", V_AGE: 61, V_GENDER: "Male", V_RACE: "ASIAN", V_ARM: "ARM-B", recordStatus: "Locked" },
                { recordId: 613, studyKey: "STUDY", subjectKey: "SUB3", formKey: "F_DM", V_AGE: 47, V_GENDER: "Female", V_RACE: "BLACK", V_ARM: "PLACEBO", recordStatus: "Locked" },
                { recordId: 614, studyKey: "STUDY", subjectKey: "SUB4", formKey: "F_DM", V_AGE: 58, V_GENDER: "Male", V_RACE: "WHITE", V_ARM: "ARM-A", recordStatus: "Locked" }
            ],
            AE: [
                { recordId: 621, studyKey: "STUDY", subjectKey: "SUB2", formKey: "F_AE", V_AETERM: "Headache mild", V_AESTDTC: "2026-10-18", V_AESEV: "MILD", V_AESER: false, V_AEREL: "POSSIBLE", recordStatus: "Verified" },
                { recordId: 622, studyKey: "STUDY", subjectKey: "SUB4", formKey: "F_AE", V_AETERM: "Nausea grade 1", V_AESTDTC: "2026-11-06", V_AESEV: "MILD", V_AESER: false, V_AEREL: "RELATED", recordStatus: "Verified" },
                { recordId: 623, studyKey: "STUDY", subjectKey: "SUB1", formKey: "F_AE", V_AETERM: "Fatigue transient", V_AESTDTC: "2026-10-25", V_AESEV: "MODERATE", V_AESER: false, V_AEREL: "UNRELATED", recordStatus: "Verified" }
            ],
            LB: [
                { recordId: 631, studyKey: "STUDY", subjectKey: "SUB1", formKey: "F_LB", V_LBTEST: "HEMOGLOBIN", V_LBORRES: 14.2, V_LBORRESU: "g/dL", V_LBNRIND: "NORMAL", recordStatus: "Verified" },
                { recordId: 632, studyKey: "STUDY", subjectKey: "SUB2", formKey: "F_LB", V_LBTEST: "POTASSIUM", V_LBORRES: 4.1, V_LBORRESU: "mmol/L", V_LBNRIND: "NORMAL", recordStatus: "Verified" },
                { recordId: 633, studyKey: "STUDY", subjectKey: "SUB3", formKey: "F_LB", V_LBTEST: "ALT", V_LBORRES: 32.0, V_LBORRESU: "U/L", V_LBNRIND: "NORMAL", recordStatus: "Verified" },
                { recordId: 634, studyKey: "STUDY", subjectKey: "SUB4", formKey: "F_LB", V_LBTEST: "CREATININE", V_LBORRES: 1.05, V_LBORRESU: "mg/dL", V_LBNRIND: "NORMAL", recordStatus: "Verified" }
            ]
        },
        queries: [
            {
                studyKey: "STUDY",
                subjectId: 201,
                annotationId: 701,
                description: "Please verify demographic details.",
                recordId: 601,
                variable: "V_AGE",
                subjectKey: "SUB1",
                dateCreated: "2026-01-12T10:00:00Z",
                dateModified: "2026-01-12T10:00:00Z",
                annotationType: "subject",
                subjectOid: "SE_SUB1",
                type: "Query",
                lastUpdated: "2026-01-12T10:00:00Z",
                queryComments: [
                    {
                        sequence: 1,
                        annotationStatus: "Monitor Open",
                        user: "sim_monitor",
                        comment: "Please verify demographic details.",
                        closed: false,
                        date: "2026-01-12T10:00:00Z"
                    }
                ]
            },
            {
                studyKey: "STUDY",
                subjectId: 201,
                annotationId: 702,
                description: "Check vital sign measurement.",
                recordId: 601,
                variable: "V_WEIGHT",
                subjectKey: "SUB1",
                dateCreated: "2026-01-12T10:00:00Z",
                dateModified: "2026-01-12T10:00:00Z",
                annotationType: "record",
                subjectOid: "SE_SUB1",
                type: "Query",
                lastUpdated: "2026-01-12T10:00:00Z",
                queryComments: [
                    {
                        sequence: 1,
                        annotationStatus: "Monitor Open",
                        user: "sim_monitor",
                        comment: "Check vital sign measurement.",
                        closed: false,
                        date: "2026-01-12T10:00:00Z"
                    }
                ]
            },
            {
                studyKey: "STUDY",
                subjectId: 202,
                annotationId: 703,
                description: "Repeat systolic reading confirmed after 10-minute rest.",
                recordId: 602,
                variable: "V_SYSBP",
                subjectKey: "SUB2",
                dateCreated: "2026-01-14T11:00:00Z",
                dateModified: "2026-01-14T11:30:00Z",
                annotationType: "record",
                subjectOid: "SE_SUB2",
                type: "Query",
                lastUpdated: "2026-01-14T11:30:00Z",
                queryComments: [
                    {
                        sequence: 1,
                        annotationStatus: "Subject Answered",
                        user: "sim_coordinator",
                        comment: "Repeat systolic reading confirmed after 10-minute rest.",
                        closed: false,
                        date: "2026-01-14T11:30:00Z"
                    }
                ]
            },
            {
                studyKey: "STUDY",
                subjectId: 203,
                annotationId: 704,
                description: "Confirmed unit notation matches study data dictionary.",
                recordId: 603,
                variable: "V_PULSE",
                subjectKey: "SUB3",
                dateCreated: "2026-01-15T16:20:00Z",
                dateModified: "2026-01-15T16:45:00Z",
                annotationType: "record",
                subjectOid: "SE_SUB3",
                type: "Query",
                lastUpdated: "2026-01-15T16:45:00Z",
                queryComments: [
                    {
                        sequence: 1,
                        annotationStatus: "Monitor Closed",
                        user: "sim_monitor",
                        comment: "Confirmed unit notation matches study data dictionary.",
                        closed: true,
                        date: "2026-01-15T16:45:00Z"
                    }
                ]
            }
        ],
        audits: [
            {
                auditId: "AUD-8801",
                timestamp: "2026-11-15T18:22:10Z",
                user: "sim_admin",
                role: "Clinical Data Manager",
                action: "RECORD_VERIFY",
                target: "STUDY/F_VS/601",
                hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                wcagCompliance: "PASS",
                status: "VERIFIED"
            },
            {
                auditId: "AUD-8802",
                timestamp: "2026-11-15T18:35:45Z",
                user: "sim_user",
                role: "Principal Investigator",
                action: "ELECTRONIC_SIGNATURE",
                target: "STUDY/F_VS/601",
                hash: "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4",
                wcagCompliance: "PASS",
                status: "VERIFIED"
            },
            {
                auditId: "AUD-8803",
                timestamp: "2026-11-15T19:01:12Z",
                user: "sim_sync_job",
                role: "Automated Pipeline",
                action: "DATA_EXPORT_DUCKDB",
                target: "STUDY/PARQUET",
                hash: "7d793037a0760186574b0282f2f435e7b1e50774690f69799143a5544e131536",
                wcagCompliance: "PASS",
                status: "VERIFIED"
            }
        ],
        vpat: [
            {
                criteria: "Principle 1: Perceivable",
                standard: "WCAG 2.1 Level AA",
                status: "PASS",
                wcagCompliance: "PASS",
                notes: "Unofficial open-source imednet-toolkit portal & TUI: Furo tokens (>= 4.5:1 AA, >= 7:1 prefers-contrast: more), forced-colors support, titled Mermaid SVGs, and tabular fallbacks"
            },
            {
                criteria: "Principle 2: Operable",
                standard: "WCAG 2.1 Level AA",
                status: "PASS",
                wcagCompliance: "PASS",
                notes: "Roving tabindex tablists (Arrow, Home, End), visible 2px focus-visible rings, keyboard-scrollable code/diagram regions, and full keyboard TUI bindings"
            },
            {
                criteria: "Principle 3: Understandable",
                standard: "WCAG 2.1 Level AA",
                status: "PASS",
                wcagCompliance: "PASS",
                notes: "SC 2.5.3 Label-in-Name control labels, polite ARIA live simulation preview status announcements, and clear CLI/TUI error diagnostics"
            },
            {
                criteria: "Principle 4: Robust",
                standard: "WCAG 2.1 Level AA",
                status: "PASS",
                wcagCompliance: "PASS",
                notes: "Community-maintained open-source SDK (not affiliated with Mednet Solutions); verified in CI via Chromium Playwright — report issues at https://github.com/fderuiter/imednet-toolkit/issues"
            }
        ]
    };

    // -------------------------------------------------------------------------
    // Clinical Data Manager Variable Validation Metadata (keyed by Form domain)
    // -------------------------------------------------------------------------
    const CDM_FORM_SCHEMAS = {
        VS: {
            domain: "VS",
            summary: "Standard vital signs observations (blood pressure, pulse, weight, temperature).",
            variables: [
                { id: "SUBJID", label: "Subject Identifier", type: "string", required: true, rule: "Matches SUBX" },
                { id: "VISIT", label: "Visit Name", type: "string", required: true, rule: "Values: Baseline, Follow-Up" },
                { id: "V_SYSBP", label: "Systolic Blood Pressure (mmHg)", type: "integer", required: true, rule: "70 <= val <= 240" },
                { id: "V_DIABP", label: "Diastolic Blood Pressure (mmHg)", type: "integer", required: true, rule: "40 <= val <= 140" },
                { id: "V_PULSE", label: "Pulse Rate (beats/min)", type: "integer", required: true, rule: "40 <= val <= 180" },
                { id: "V_WEIGHT", label: "Weight (kg)", type: "float", required: true, rule: "val > 0.0" },
                { id: "V_TEMP", label: "Oral Temperature (°C)", type: "float", required: false, rule: "35.0 <= val <= 41.0" }
            ]
        },
        DM: {
            domain: "DM",
            summary: "Demographic background, biological sex, race category, and randomized arm.",
            variables: [
                { id: "SUBJID", label: "Subject Identifier", type: "string", required: true, rule: "Matches SUBX" },
                { id: "V_AGE", label: "Age at Screening", type: "integer", required: true, rule: "val >= 18" },
                { id: "V_GENDER", label: "Biological Sex", type: "string", required: true, rule: "Values: Female, Male, Other" },
                { id: "V_RACE", label: "Race Category", type: "string", required: true, rule: "CDISC controlled terminology" },
                { id: "V_ARM", label: "Planned Arm", type: "string", required: true, rule: "Values: ARM-A, ARM-B, PLACEBO" }
            ]
        },
        AE: {
            domain: "AE",
            summary: "Clinical safety observation terms, severity grading, and relationship to study agent.",
            variables: [
                { id: "SUBJID", label: "Subject Identifier", type: "string", required: true, rule: "Matches SUBX" },
                { id: "V_AETERM", label: "Reported Observation Term", type: "string", required: true, rule: "MedDRA LLT terminology" },
                { id: "V_AESTDTC", label: "Start Date", type: "date", required: true, rule: "ISO 8601 YYYY-MM-DD" },
                { id: "V_AESEV", label: "Severity Grade", type: "string", required: true, rule: "Values: MILD, MODERATE, SEVERE" },
                { id: "V_AESER", label: "Serious Safety Indicator", type: "boolean", required: true, rule: "true | false" },
                { id: "V_AEREL", label: "Causality Relationship", type: "string", required: true, rule: "Values: RELATED, POSSIBLE, UNRELATED" }
            ]
        },
        LB: {
            domain: "LB",
            summary: "Laboratory analytes, standard units, and reference range indicators.",
            variables: [
                { id: "SUBJID", label: "Subject Identifier", type: "string", required: true, rule: "Matches SUBX" },
                { id: "V_LBTEST", label: "Analyte Test Name", type: "string", required: true, rule: "e.g. HEMOGLOBIN, POTASSIUM, ALT" },
                { id: "V_LBORRES", label: "Numeric Result", type: "float", required: true, rule: "val > 0.0" },
                { id: "V_LBORRESU", label: "Standard Unit", type: "string", required: true, rule: "SI units (g/dL, mmol/L, U/L)" },
                { id: "V_LBNRIND", label: "Normal Range Indicator", type: "string", required: true, rule: "Values: NORMAL, LOW, HIGH" }
            ]
        }
    };

    // -------------------------------------------------------------------------
    // Verified read-only scenario snippets (offline simulation mode)
    // -------------------------------------------------------------------------
    const SNIPPETS = {
        sdk: {
            records: `# Query study records and export to pandas DataFrame\nfrom imednet import ImednetSDK\n\nwith ImednetSDK(simulation_mode=True) as sdk:\n    records = list(sdk.records.list(study_key="STUDY"))\n    df = sdk.records.to_dataframe(\n        study_key="STUDY",\n        flatten=True,\n    )\n    print(df.head())`,
            studies: `# List available clinical studies\nfrom imednet import ImednetSDK\n\nwith ImednetSDK(simulation_mode=True) as sdk:\n    studies = list(sdk.studies.list())\n    for s in studies:\n        print(f"{s.study_key}: {s.study_name} ({s.study_type})")`,
            forms: `# List study forms and metadata\nfrom imednet import ImednetSDK\n\nwith ImednetSDK(simulation_mode=True) as sdk:\n    forms = list(sdk.forms.list(study_key="STUDY"))\n    for f in forms:\n        print(f"{f.form_name} ({f.form_key})")`,
            queries: `# Inspect open study queries by variable\nfrom imednet import ImednetSDK\nfrom imednet.models.queries import QueryStatus\n\nwith ImednetSDK(simulation_mode=True) as sdk:\n    queries = [\n        q for q in sdk.queries.list(study_key="STUDY")\n        if q.status == QueryStatus.OPEN\n    ]\n    print(f"Open queries: {len(queries)}")`
        },
        rest: {
            records: `GET /api/v1/edc/studies/STUDY/records?size=10 HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json`,
            studies: `GET /api/v1/edc/studies HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json`,
            forms: `GET /api/v1/edc/studies/STUDY/forms HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json`,
            queries: `GET /api/v1/edc/studies/STUDY/queries HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json`
        }
    };

    // -------------------------------------------------------------------------
    // Helper: Escape HTML
    // -------------------------------------------------------------------------
    function escapeHtml(str) {
        if (str === null || str === undefined) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    // -------------------------------------------------------------------------
    // Helper: Copy text safely with button status reset
    // -------------------------------------------------------------------------
    function bindCopyButton(btn, getText, defaultLabel) {
        if (!btn) return;
        let resetTimer = null;
        btn.addEventListener("click", function () {
            const text = getText();
            if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
                return;
            }
            navigator.clipboard.writeText(text).then(function () {
                btn.textContent = "Copied!";
                if (resetTimer) {
                    clearTimeout(resetTimer);
                }
                resetTimer = setTimeout(function () {
                    btn.textContent = defaultLabel;
                    resetTimer = null;
                }, 1500);
            }).catch(function () {});
        });
    }

    // -------------------------------------------------------------------------
    // Helper: Render Tabular DataFrame view
    // -------------------------------------------------------------------------
    function renderDataFrameTable(items, captionText) {
        if (!items || !items.length) {
            return `<div class="explorer-empty-state">No records found.</div>`;
        }

        const keys = Object.keys(items[0]);
        let html = `<div class="explorer-table-wrapper" tabindex="0" role="region" aria-label="${escapeHtml(captionText)}">`;
        html += `<table class="explorer-dataframe-table">`;
        if (captionText) {
            html += `<caption>${escapeHtml(captionText)} — ${items.length} items</caption>`;
        }
        html += `<thead><tr>`;
        html += `<th scope="col" class="index-cell">#</th>`;
        keys.forEach(function (k) {
            html += `<th scope="col">${escapeHtml(k)}</th>`;
        });
        html += `</tr></thead><tbody>`;

        items.forEach(function (record, index) {
            html += `<tr>`;
            html += `<td class="index-cell">${index}</td>`;
            keys.forEach(function (k) {
                const val = record[k];
                let cellHtml = escapeHtml(val);
                if (k === "queryComments" && Array.isArray(val)) {
                    const latest = val.length ? val[val.length - 1] : null;
                    const statusLabel = latest && latest.annotationStatus ? String(latest.annotationStatus) : "Open";
                    const lowerStatus = statusLabel.toLowerCase();
                    const commentBadge = lowerStatus.includes("closed")
                        ? "badge-success"
                        : lowerStatus.includes("open") || lowerStatus.includes("answered")
                        ? "badge-warning"
                        : "badge-neutral";
                    cellHtml = `<span class="status-badge ${commentBadge}">${escapeHtml(statusLabel)}</span> <code>(${val.length})</code>`;
                } else if (k === "STATUS" || k === "status" || k === "recordStatus" || k === "wcagCompliance") {
                    const lowerVal = String(val).toLowerCase();
                    const badgeClass = lowerVal.includes("verified") || lowerVal.includes("pass") || lowerVal.includes("complete") || lowerVal.includes("locked")
                        ? "badge-success"
                        : lowerVal.includes("open") || lowerVal.includes("answered")
                        ? "badge-warning"
                        : "badge-neutral";
                    cellHtml = `<span class="status-badge ${badgeClass}">${cellHtml}</span>`;
                } else if (typeof val === "boolean") {
                    cellHtml = val ? `<span class="status-badge badge-success">true</span>` : `<span class="status-badge badge-neutral">false</span>`;
                } else if (val !== null && typeof val === "object") {
                    cellHtml = `<code>${escapeHtml(JSON.stringify(val))}</code>`;
                }
                html += `<td>${cellHtml}</td>`;
            });
            html += `</tr>`;
        });

        html += `</tbody></table></div>`;
        return html;
    }

    // -------------------------------------------------------------------------
    // Helper: Render Form Variable Structure
    // -------------------------------------------------------------------------
    function renderCrfStructure(formDef, formSchema) {
        if (!formDef || !formSchema) return "";
        let html = `<div class="crf-view-container">`;
        html += `<div class="crf-header">`;
        html += `<h4><span class="crf-tag">${escapeHtml(formSchema.domain)}</span> ${escapeHtml(formDef.formName)} <span class="crf-oid">(${escapeHtml(formDef.formKey || formDef.formId)})</span></h4>`;
        html += `<p class="crf-desc">${escapeHtml(formSchema.summary)}</p>`;
        html += `</div>`;

        html += `<div class="form-variables-grid">`;
        formSchema.variables.forEach(function (variable) {
            html += `<div class="form-variable-card">`;
            html += `<div class="form-variable-header">`;
            html += `<span class="form-variable-id">${escapeHtml(variable.id)}</span>`;
            if (variable.required) {
                html += `<span class="crf-badge-required">Required</span>`;
            } else {
                html += `<span class="crf-badge-optional">Optional</span>`;
            }
            html += `</div>`;
            html += `<div class="form-variable-label">${escapeHtml(variable.label)}</div>`;
            html += `<div class="form-variable-meta">`;
            html += `<span class="crf-meta-item"><strong>Type:</strong> <code>${escapeHtml(variable.type)}</code></span>`;
            html += `<span class="crf-meta-item"><strong>Rule:</strong> <code>${escapeHtml(variable.rule)}</code></span>`;
            html += `</div>`;
            html += `</div>`;
        });
        html += `</div></div>`;
        return html;
    }

    // -------------------------------------------------------------------------
    // Keyboard focus management for overflowing preformatted code blocks
    // -------------------------------------------------------------------------
    let codeBlocksRaf = null;

    function checkCodeBlocks() {
        const blocks = document.querySelectorAll("pre");
        blocks.forEach(function (block) {
            const isOverflowing = block.clientWidth > 0 && block.scrollWidth > block.clientWidth;
            if (isOverflowing) {
                block.setAttribute("tabindex", "0");
                block.setAttribute("role", "region");
                if (!block.getAttribute("aria-label")) {
                    block.setAttribute("aria-label", "Code snippet");
                }
            } else {
                if (block.hasAttribute("tabindex")) {
                    block.removeAttribute("tabindex");
                }
                if (block.getAttribute("role") === "region" && block.getAttribute("aria-label") === "Code snippet") {
                    block.removeAttribute("role");
                    block.removeAttribute("aria-label");
                }
            }
        });

        const mermaidBlocks = document.querySelectorAll(".mermaid");
        mermaidBlocks.forEach(function (diagram) {
            const svg = typeof diagram.querySelector === "function" ? diagram.querySelector("svg") : null;
            let titleText = "";
            if (svg && typeof svg.querySelector === "function") {
                const titleEl = svg.querySelector("title");
                if (titleEl && titleEl.textContent) {
                    titleText = titleEl.textContent.trim();
                }
                if (titleText && !svg.getAttribute("aria-label")) {
                    svg.setAttribute("aria-label", titleText);
                }
            }
            if (titleText) {
                diagram.setAttribute("data-mermaid-label", titleText);
            }
            const regionLabel = titleText || diagram.getAttribute("data-mermaid-label") || "Mermaid diagram";
            const isOverflowing = diagram.clientWidth > 0 && diagram.scrollWidth > diagram.clientWidth;
            if (isOverflowing) {
                diagram.setAttribute("tabindex", "0");
                diagram.setAttribute("role", "region");
                if (!diagram.getAttribute("aria-label") || diagram.hasAttribute("data-mermaid-region")) {
                    diagram.setAttribute("aria-label", regionLabel);
                    diagram.setAttribute("data-mermaid-region", "true");
                }
            } else {
                if (diagram.hasAttribute("tabindex")) {
                    diagram.removeAttribute("tabindex");
                }
                if (diagram.hasAttribute("data-mermaid-region")) {
                    diagram.removeAttribute("role");
                    diagram.removeAttribute("aria-label");
                    diagram.removeAttribute("data-mermaid-region");
                }
            }
        });
    }

    function scheduleCheckCodeBlocks() {
        if (typeof cancelAnimationFrame === "function" && codeBlocksRaf) {
            cancelAnimationFrame(codeBlocksRaf);
            codeBlocksRaf = null;
        }
        if (typeof requestAnimationFrame === "function") {
            codeBlocksRaf = requestAnimationFrame(function () {
                codeBlocksRaf = null;
                checkCodeBlocks();
            });
        } else {
            checkCodeBlocks();
        }
    }

    // -------------------------------------------------------------------------
    // Shared WAI-ARIA Tablist Binder (ArrowRight/Left/Down/Up, Home, End + roving tabindex)
    // -------------------------------------------------------------------------
    function syncTablistRovingTabindex(tabs) {
        const tabArray = Array.from(tabs || []).filter(Boolean);
        if (!tabArray.length) return;
        let hasSelected = false;
        tabArray.forEach(function (tab) {
            const selected = tab.getAttribute("aria-selected") === "true";
            if (selected) hasSelected = true;
            tab.setAttribute("tabindex", selected ? "0" : "-1");
        });
        if (!hasSelected) {
            tabArray[0].setAttribute("tabindex", "0");
        }
    }

    function bindTablist(tabs, onActivate) {
        const tabArray = Array.from(tabs || []).filter(Boolean);
        if (!tabArray.length) return;
        syncTablistRovingTabindex(tabArray);

        tabArray.forEach(function (tab, index) {
            tab.addEventListener("click", function () {
                onActivate(tab, index);
                syncTablistRovingTabindex(tabArray);
            });

            tab.addEventListener("keydown", function (e) {
                let targetIndex = null;
                if (e.key === "ArrowRight" || e.key === "ArrowDown") {
                    targetIndex = (index + 1) % tabArray.length;
                } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
                    targetIndex = (index - 1 + tabArray.length) % tabArray.length;
                } else if (e.key === "Home") {
                    targetIndex = 0;
                } else if (e.key === "End") {
                    targetIndex = tabArray.length - 1;
                }

                if (targetIndex !== null) {
                    e.preventDefault();
                    const nextTab = tabArray[targetIndex];
                    onActivate(nextTab, targetIndex);
                    syncTablistRovingTabindex(tabArray);
                    if (typeof nextTab.focus === "function") {
                        nextTab.focus();
                    }
                }
            });
        });
    }

    // -------------------------------------------------------------------------
    // Main Initialization
    // -------------------------------------------------------------------------
    function initExplorer() {
        const root = document.getElementById("interactive-showcase-root");
        if (!root) return; // Not on page or already initialized

        // Persona tab switching
        const personaTabs = root.querySelectorAll(".showcase-persona-tab");
        const personaPanels = root.querySelectorAll(".showcase-panel");

        function isValidPanel(targetId) {
            if (!targetId) return false;
            for (let i = 0; i < personaPanels.length; i++) {
                if (personaPanels[i].id === targetId) return true;
            }
            return false;
        }

        function switchPersona(targetId, updateHistory) {
            if (updateHistory === undefined) {
                updateHistory = true;
            }
            const activeId = isValidPanel(targetId) ? targetId : "panel-integration";

            personaTabs.forEach(function (tab) {
                const isActive = tab.getAttribute("data-target") === activeId;
                tab.setAttribute("aria-selected", isActive ? "true" : "false");
                tab.setAttribute("tabindex", isActive ? "0" : "-1");
                tab.classList.toggle("active", isActive);
            });

            personaPanels.forEach(function (panel) {
                const isMatch = panel.id === activeId;
                panel.classList.toggle("active", isMatch);
                if (isMatch) {
                    panel.removeAttribute("hidden");
                } else {
                    panel.setAttribute("hidden", "true");
                }
            });

            if (updateHistory && window.history && typeof window.history.replaceState === "function") {
                window.history.replaceState(null, "", "#" + activeId);
            }

            scheduleCheckCodeBlocks();
        }

        function getHashTarget() {
            if (!window.location.hash) return null;
            return window.location.hash.replace(/^#/, "");
        }

        // Initialize persona from URL hash (safely falling back to default panel-integration)
        const initialHash = getHashTarget();
        if (initialHash && isValidPanel(initialHash)) {
            switchPersona(initialHash, false);
            const targetEl = document.getElementById(initialHash);
            if (targetEl && typeof targetEl.scrollIntoView === "function") {
                targetEl.scrollIntoView();
            }
        } else {
            switchPersona("panel-integration", false);
        }

        window.addEventListener("hashchange", function () {
            const currentHash = getHashTarget();
            if (isValidPanel(currentHash)) {
                switchPersona(currentHash, false);
            } else {
                switchPersona("panel-integration", false);
            }
        });

        bindTablist(personaTabs, function (tab) {
            const targetId = tab.getAttribute("data-target");
            switchPersona(targetId);
        });

        // External triggers (e.g. from role cards at top of portal)
        document.querySelectorAll("[data-persona-target]").forEach(function (trigger) {
            trigger.addEventListener("click", function (e) {
                e.preventDefault();
                const target = trigger.getAttribute("data-persona-target");
                switchPersona(target);
                const el = document.getElementById("interactive-showcase-root");
                if (el && el.scrollIntoView) {
                    el.scrollIntoView({ behavior: "smooth", block: "start" });
                }
            });
        });

        // ---------------------------------------------------------------------
        // 1. Integration Engineer Panel (Read-Only Scenario Explorer)
        // ---------------------------------------------------------------------
        const ieSelectDataset = root.querySelector("#ie-dataset-select");
        const ieModeSdk = root.querySelector("#ie-mode-sdk");
        const ieModeRest = root.querySelector("#ie-mode-rest");
        const ieCodeDisplay = root.querySelector("#ie-code-display");
        const ieSnippetCopyBtn = root.querySelector("#ie-snippet-copy-btn");
        const ieMetricBadge = root.querySelector("#ie-metric-badge");
        const ieOutputTabTable = root.querySelector("#ie-tab-table");
        const ieOutputTabJson = root.querySelector("#ie-tab-json");
        const ieViewTable = root.querySelector("#ie-view-table");
        const ieViewJson = root.querySelector("#ie-view-json");
        const ieJsonPre = root.querySelector("#ie-json-pre code");
        const ieCopyBtn = root.querySelector("#ie-copy-btn");

        let currentIeMode = "sdk";

        function updateIeSnippet() {
            if (!ieSelectDataset || !ieCodeDisplay) return;
            const ds = ieSelectDataset.value;
            const snippet = SNIPPETS[currentIeMode][ds] || "";
            ieCodeDisplay.textContent = snippet;
            ieCodeDisplay.className = currentIeMode === "sdk" ? "language-python" : "language-http";
            scheduleCheckCodeBlocks();
        }

        function executeIntegrationQuery() {
            const datasetKey = ieSelectDataset ? ieSelectDataset.value : "records";
            let data = [];
            let caption = "STUDY Records (Vital Signs Form)";

            if (datasetKey === "records") {
                data = MOCK_DATA.records.VS;
                caption = "pandas.DataFrame: STUDY / F_VS (Vital Signs Records)";
            } else if (datasetKey === "studies") {
                data = MOCK_DATA.studies;
                caption = "Clinical Studies Catalog";
            } else if (datasetKey === "forms") {
                data = Object.keys(MOCK_DATA.forms).map(function (k) {
                    return MOCK_DATA.forms[k];
                });
                caption = "Study Forms Catalog (STUDY)";
            } else if (datasetKey === "queries") {
                data = MOCK_DATA.queries;
                caption = "Study Queries Ledger (STUDY)";
            }

            if (ieMetricBadge) {
                ieMetricBadge.textContent = `Simulation Preview (Deterministic Fixture) — ${caption} (${data.length} items)`;
                ieMetricBadge.classList.add("active");
            }

            if (ieViewTable) {
                ieViewTable.innerHTML = renderDataFrameTable(data, caption);
            }

            if (ieJsonPre) {
                ieJsonPre.textContent = JSON.stringify(data, null, 2);
            }
            scheduleCheckCodeBlocks();
        }

        if (ieSelectDataset) {
            ieSelectDataset.addEventListener("change", function () {
                updateIeSnippet();
                executeIntegrationQuery();
            });
        }

        if (ieModeSdk && ieModeRest) {
            ieModeSdk.addEventListener("click", function () {
                currentIeMode = "sdk";
                ieModeSdk.classList.add("active");
                ieModeSdk.setAttribute("aria-pressed", "true");
                ieModeRest.classList.remove("active");
                ieModeRest.setAttribute("aria-pressed", "false");
                updateIeSnippet();
                executeIntegrationQuery();
            });
            ieModeRest.addEventListener("click", function () {
                currentIeMode = "rest";
                ieModeRest.classList.add("active");
                ieModeRest.setAttribute("aria-pressed", "true");
                ieModeSdk.classList.remove("active");
                ieModeSdk.setAttribute("aria-pressed", "false");
                updateIeSnippet();
                executeIntegrationQuery();
            });
        }

        bindCopyButton(ieSnippetCopyBtn, function () {
            return ieCodeDisplay ? ieCodeDisplay.textContent : "";
        }, "Copy Code");

        // View toggle for IE
        function switchIeView(view) {
            if (view === "table") {
                ieOutputTabTable.classList.add("active");
                ieOutputTabTable.setAttribute("aria-selected", "true");
                ieOutputTabTable.setAttribute("tabindex", "0");
                ieOutputTabJson.classList.remove("active");
                ieOutputTabJson.setAttribute("aria-selected", "false");
                ieOutputTabJson.setAttribute("tabindex", "-1");
                ieViewTable.removeAttribute("hidden");
                ieViewJson.setAttribute("hidden", "true");
            } else {
                ieOutputTabJson.classList.add("active");
                ieOutputTabJson.setAttribute("aria-selected", "true");
                ieOutputTabJson.setAttribute("tabindex", "0");
                ieOutputTabTable.classList.remove("active");
                ieOutputTabTable.setAttribute("aria-selected", "false");
                ieOutputTabTable.setAttribute("tabindex", "-1");
                ieViewJson.removeAttribute("hidden");
                ieViewTable.setAttribute("hidden", "true");
            }
            scheduleCheckCodeBlocks();
        }

        if (ieOutputTabTable && ieOutputTabJson) {
            bindTablist([ieOutputTabTable, ieOutputTabJson], function (tab) {
                switchIeView(tab === ieOutputTabTable ? "table" : "json");
            });
        }

        bindCopyButton(ieCopyBtn, function () {
            return ieJsonPre ? ieJsonPre.textContent : "";
        }, "Copy JSON");

        // ---------------------------------------------------------------------
        // 2. Clinical Data Manager Panel
        // ---------------------------------------------------------------------
        const cdmSelectForm = root.querySelector("#cdm-form-select");
        const cdmValidateBtn = root.querySelector("#cdm-validate-btn");
        const cdmMetricBadge = root.querySelector("#cdm-metric-badge");
        const cdmOutputTabCrf = root.querySelector("#cdm-tab-crf");
        const cdmOutputTabJson = root.querySelector("#cdm-tab-json");
        const cdmViewCrf = root.querySelector("#cdm-view-crf");
        const cdmViewJson = root.querySelector("#cdm-view-json");
        const cdmJsonPre = root.querySelector("#cdm-json-pre code");
        const cdmCopyBtn = root.querySelector("#cdm-copy-btn");

        function executeCdmValidation() {
            const formKey = cdmSelectForm ? cdmSelectForm.value : "VS";
            const formDef = MOCK_DATA.forms[formKey] || MOCK_DATA.forms.VS;
            const formSchema = CDM_FORM_SCHEMAS[formKey] || CDM_FORM_SCHEMAS.VS;

            if (cdmMetricBadge) {
                cdmMetricBadge.textContent = `Simulation Preview (Deterministic Fixture) — ${formDef.formName} (${formSchema.variables.length} Variables) | CDISC ODM Compliant`;
                cdmMetricBadge.classList.add("active");
            }

            if (cdmViewCrf) {
                cdmViewCrf.innerHTML = renderCrfStructure(formDef, formSchema);
            }

            if (cdmJsonPre) {
                cdmJsonPre.textContent = JSON.stringify(formDef, null, 2);
            }
            scheduleCheckCodeBlocks();
        }

        if (cdmValidateBtn) {
            cdmValidateBtn.addEventListener("click", executeCdmValidation);
        }
        if (cdmSelectForm) {
            cdmSelectForm.addEventListener("change", executeCdmValidation);
        }

        function switchCdmView(view) {
            if (view === "crf") {
                cdmOutputTabCrf.classList.add("active");
                cdmOutputTabCrf.setAttribute("aria-selected", "true");
                cdmOutputTabCrf.setAttribute("tabindex", "0");
                cdmOutputTabJson.classList.remove("active");
                cdmOutputTabJson.setAttribute("aria-selected", "false");
                cdmOutputTabJson.setAttribute("tabindex", "-1");
                cdmViewCrf.removeAttribute("hidden");
                cdmViewJson.setAttribute("hidden", "true");
            } else {
                cdmOutputTabJson.classList.add("active");
                cdmOutputTabJson.setAttribute("aria-selected", "true");
                cdmOutputTabJson.setAttribute("tabindex", "0");
                cdmOutputTabCrf.classList.remove("active");
                cdmOutputTabCrf.setAttribute("aria-selected", "false");
                cdmOutputTabCrf.setAttribute("tabindex", "-1");
                cdmViewJson.removeAttribute("hidden");
                cdmViewCrf.setAttribute("hidden", "true");
            }
            scheduleCheckCodeBlocks();
        }

        if (cdmOutputTabCrf && cdmOutputTabJson) {
            bindTablist([cdmOutputTabCrf, cdmOutputTabJson], function (tab) {
                switchCdmView(tab === cdmOutputTabCrf ? "crf" : "json");
            });
        }

        bindCopyButton(cdmCopyBtn, function () {
            return cdmJsonPre ? cdmJsonPre.textContent : "";
        }, "Copy JSON");

        // ---------------------------------------------------------------------
        // 3. Regulatory Compliance Auditor Panel
        // ---------------------------------------------------------------------
        const audScenarioSelect = root.querySelector("#aud-scenario-select");
        const audVerifyBtn = root.querySelector("#aud-verify-btn");
        const audMetricBadge = root.querySelector("#aud-metric-badge");
        const audOutputTabTable = root.querySelector("#aud-tab-table");
        const audOutputTabJson = root.querySelector("#aud-tab-json");
        const audViewTable = root.querySelector("#aud-view-table");
        const audViewJson = root.querySelector("#aud-view-json");
        const audJsonPre = root.querySelector("#aud-json-pre code");
        const audCopyBtn = root.querySelector("#aud-copy-btn");

        function executeAuditorVerification() {
            const scenario = audScenarioSelect ? audScenarioSelect.value : "part11";
            if (scenario !== "part11" && scenario !== "vpat") {
                // Selecting an unmapped scenario must preserve existing table contents and avoid throwing script exceptions.
                return;
            }

            let data = [];
            let caption = "";
            let metricText = "";

            if (scenario === "part11") {
                data = MOCK_DATA.audits;
                caption = "21 CFR Part 11 Audit Trail & Accessibility Log";
                metricText = "Simulation Preview (Deterministic Fixture) — 21 CFR Part 11 Electronic Signature & VPAT Verified";
            } else if (scenario === "vpat") {
                data = MOCK_DATA.vpat;
                caption = "WCAG 2.1 AA Accessibility Conformance Verification";
                metricText = "Simulation Preview (Deterministic Fixture) — WCAG 2.1 AA Accessibility Conformance (VPAT) Verified";
            }

            if (audMetricBadge) {
                audMetricBadge.textContent = metricText;
                audMetricBadge.classList.add("active");
            }

            if (audViewTable) {
                audViewTable.innerHTML = renderDataFrameTable(data, caption);
            }

            if (audJsonPre) {
                audJsonPre.textContent = JSON.stringify(data, null, 2);
            }
            scheduleCheckCodeBlocks();
        }

        if (audVerifyBtn) {
            audVerifyBtn.addEventListener("click", executeAuditorVerification);
        }
        if (audScenarioSelect) {
            audScenarioSelect.addEventListener("change", executeAuditorVerification);
        }

        function switchAudView(view) {
            if (view === "table") {
                audOutputTabTable.classList.add("active");
                audOutputTabTable.setAttribute("aria-selected", "true");
                audOutputTabTable.setAttribute("tabindex", "0");
                audOutputTabJson.classList.remove("active");
                audOutputTabJson.setAttribute("aria-selected", "false");
                audOutputTabJson.setAttribute("tabindex", "-1");
                audViewTable.removeAttribute("hidden");
                audViewJson.setAttribute("hidden", "true");
            } else {
                audOutputTabJson.classList.add("active");
                audOutputTabJson.setAttribute("aria-selected", "true");
                audOutputTabJson.setAttribute("tabindex", "0");
                audOutputTabTable.classList.remove("active");
                audOutputTabTable.setAttribute("aria-selected", "false");
                audOutputTabTable.setAttribute("tabindex", "-1");
                audViewJson.removeAttribute("hidden");
                audViewTable.setAttribute("hidden", "true");
            }
            scheduleCheckCodeBlocks();
        }

        if (audOutputTabTable && audOutputTabJson) {
            bindTablist([audOutputTabTable, audOutputTabJson], function (tab) {
                switchAudView(tab === audOutputTabTable ? "table" : "json");
            });
        }

        bindCopyButton(audCopyBtn, function () {
            return audJsonPre ? audJsonPre.textContent : "";
        }, "Copy JSON");

        // Render default deterministic simulation previews immediately on load
        updateIeSnippet();
        executeIntegrationQuery();
        executeCdmValidation();
        executeAuditorVerification();
    }

    // -------------------------------------------------------------------------
    // Keyboard focus management for overflowing preformatted code blocks
    // -------------------------------------------------------------------------
    function initAccessibleCodeBlocks() {
        checkCodeBlocks();
        window.addEventListener("resize", scheduleCheckCodeBlocks);
        window.addEventListener("load", scheduleCheckCodeBlocks);

        if (typeof MutationObserver !== "undefined") {
            const observer = new MutationObserver(function () {
                scheduleCheckCodeBlocks();
            });
            const target = document.body || document.documentElement;
            if (target) {
                observer.observe(target, {
                    attributes: true,
                    attributeFilter: ["hidden", "class"],
                    subtree: true
                });
            }

            const mermaidObserver = new MutationObserver(function () {
                scheduleCheckCodeBlocks();
            });
            document.querySelectorAll(".mermaid").forEach(function (diagram) {
                mermaidObserver.observe(diagram, {
                    childList: true,
                    subtree: true
                });
            });
        }
    }

    function init() {
        initExplorer();
        initAccessibleCodeBlocks();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
