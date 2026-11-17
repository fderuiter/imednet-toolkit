/**
 * Interactive Showcase & Embedded Live Explorer for iMednet Documentation Portal
 * Client-side execution engine with preloaded clinical datasets, persona tabs,
 * tabular DataFrame rendering, raw JSON viewer, and WCAG 2.1 AA accessibility.
 */

(function () {
    "use strict";

    // -------------------------------------------------------------------------
    // Preloaded Mock Datasets (De-identified synthetic data; Zero PHI)
    // -------------------------------------------------------------------------
    const MOCK_DATA = {
        studies: [
            {
                studyKey: "STUDY-101",
                name: "Phase II Oncology Biomarker Study",
                protocol: "ONC-2026-B",
                phase: "Phase II",
                status: "ACTIVE",
                sitesCount: 12,
                enrolledSubjects: 145,
                targetAccrual: 160,
                leadInvestigator: "Dr. Elena Rostova",
                createdDate: "2025-03-10T08:00:00Z"
            },
            {
                studyKey: "STUDY-202",
                name: "Cardiovascular Safety & Efficacy Assessment",
                protocol: "CVS-2025-A",
                phase: "Phase III",
                status: "ACTIVE",
                sitesCount: 8,
                enrolledSubjects: 92,
                targetAccrual: 100,
                leadInvestigator: "Dr. Marcus Vance",
                createdDate: "2025-06-15T10:30:00Z"
            },
            {
                studyKey: "STUDY-303",
                name: "Pediatric Autoimmune Biomarker Registry",
                protocol: "PED-IMM-2026",
                phase: "Phase I/II",
                status: "RECRUITING",
                sitesCount: 5,
                enrolledSubjects: 34,
                targetAccrual: 50,
                leadInvestigator: "Dr. Sarah Chen",
                createdDate: "2026-01-20T14:15:00Z"
            }
        ],
        forms: {
            VS: {
                formId: "VS",
                formName: "Vital Signs",
                domain: "VS",
                description: "Standard vital signs observations (blood pressure, pulse, temperature).",
                fields: [
                    { id: "SUBJID", label: "Subject Identifier", type: "string", required: true, rule: "Matches SUBJ-XXX" },
                    { id: "VISIT", label: "Visit Name", type: "string", required: true, rule: "Values: V1, V2, V3, EOS" },
                    { id: "VSDAT", label: "Assessment Date", type: "datetime", required: true, rule: "ISO 8601" },
                    { id: "SYSBP", label: "Systolic Blood Pressure (mmHg)", type: "integer", required: true, rule: "70 <= val <= 240" },
                    { id: "DIABP", label: "Diastolic Blood Pressure (mmHg)", type: "integer", required: true, rule: "40 <= val <= 140" },
                    { id: "PULSE", label: "Pulse Rate (beats/min)", type: "integer", required: true, rule: "40 <= val <= 180" },
                    { id: "TEMP", label: "Oral Temperature (°C)", type: "float", required: false, rule: "35.0 <= val <= 41.0" }
                ]
            },
            DM: {
                formId: "DM",
                formName: "Demographics",
                domain: "DM",
                description: "Demographic background, gender, race, and randomized treatment assignment.",
                fields: [
                    { id: "SUBJID", label: "Subject Identifier", type: "string", required: true, rule: "Matches SUBJ-XXX" },
                    { id: "AGE", label: "Age at Screening", type: "integer", required: true, rule: "val >= 18" },
                    { id: "SEX", label: "Biological Sex", type: "string", required: true, rule: "Values: M, F, OTHER" },
                    { id: "RACE", label: "Race Category", type: "string", required: true, rule: "CDISC controlled terminology" },
                    { id: "ARM", label: "Planned Arm", type: "string", required: true, rule: "Values: ARM-A, ARM-B, PLACEBO" }
                ]
            },
            AE: {
                formId: "AE",
                formName: "Adverse Events",
                domain: "AE",
                description: "Clinical adverse event terms, severity grading, and relationship to study agent.",
                fields: [
                    { id: "SUBJID", label: "Subject Identifier", type: "string", required: true, rule: "Matches SUBJ-XXX" },
                    { id: "AETERM", label: "Reported Event Term", type: "string", required: true, rule: "MedDRA LLT terminology" },
                    { id: "AESTDTC", label: "Start Date", type: "date", required: true, rule: "ISO 8601 YYYY-MM-DD" },
                    { id: "AESEV", label: "Severity Grade", type: "string", required: true, rule: "Values: MILD, MODERATE, SEVERE" },
                    { id: "AESER", label: "Serious AE Flag", type: "boolean", required: true, rule: "true | false" },
                    { id: "AEREL", label: "Causality Relationship", type: "string", required: true, rule: "Values: RELATED, POSSIBLE, UNRELATED" }
                ]
            },
            LB: {
                formId: "LB",
                formName: "Laboratory Findings",
                domain: "LB",
                description: "Central and local laboratory analytes, standard units, and reference range flags.",
                fields: [
                    { id: "SUBJID", label: "Subject Identifier", type: "string", required: true, rule: "Matches SUBJ-XXX" },
                    { id: "LBTEST", label: "Analyte Test Name", type: "string", required: true, rule: "e.g. HEMOGLOBIN, POTASSIUM, ALT" },
                    { id: "LBORRES", label: "Numeric Result", type: "float", required: true, rule: "val > 0.0" },
                    { id: "LBORRESU", label: "Standard Unit", type: "string", required: true, rule: "SI units (g/dL, mmol/L, U/L)" },
                    { id: "LBNRIND", label: "Normal Range Indicator", type: "string", required: true, rule: "Values: NORMAL, LOW, HIGH" }
                ]
            }
        },
        records: {
            VS: [
                { recordId: "REC-1001", SUBJID: "SUBJ-101", VISIT: "V1_SCREEN", VSDAT: "2026-10-12T09:15:00Z", SYSBP: 122, DIABP: 78, PULSE: 68, TEMP: 36.6, STATUS: "VERIFIED" },
                { recordId: "REC-1002", SUBJID: "SUBJ-102", VISIT: "V1_SCREEN", VSDAT: "2026-10-12T10:45:00Z", SYSBP: 138, DIABP: 86, PULSE: 74, TEMP: 36.8, STATUS: "VERIFIED" },
                { recordId: "REC-1003", SUBJID: "SUBJ-103", VISIT: "V2_WEEK4",  VSDAT: "2026-11-02T11:20:00Z", SYSBP: 118, DIABP: 74, PULSE: 62, TEMP: 36.5, STATUS: "VERIFIED" },
                { recordId: "REC-1004", SUBJID: "SUBJ-104", VISIT: "V1_SCREEN", VSDAT: "2026-11-05T14:00:00Z", SYSBP: 144, DIABP: 92, PULSE: 82, TEMP: 37.1, STATUS: "FLAGGED_QUERY" }
            ],
            DM: [
                { recordId: "REC-2001", SUBJID: "SUBJ-101", AGE: 54, SEX: "F", RACE: "WHITE", ARM: "ARM-A", STATUS: "LOCKED" },
                { recordId: "REC-2002", SUBJID: "SUBJ-102", AGE: 61, SEX: "M", RACE: "ASIAN", ARM: "ARM-B", STATUS: "LOCKED" },
                { recordId: "REC-2003", SUBJID: "SUBJ-103", AGE: 47, SEX: "F", RACE: "BLACK", ARM: "PLACEBO", STATUS: "LOCKED" },
                { recordId: "REC-2004", SUBJID: "SUBJ-104", AGE: 58, SEX: "M", RACE: "WHITE", ARM: "ARM-A", STATUS: "LOCKED" }
            ],
            AE: [
                { recordId: "REC-3001", SUBJID: "SUBJ-102", AETERM: "Headache mild", AESTDTC: "2026-10-18", AESEV: "MILD", AESER: false, AEREL: "POSSIBLE", STATUS: "VERIFIED" },
                { recordId: "REC-3002", SUBJID: "SUBJ-104", AETERM: "Nausea grade 1", AESTDTC: "2026-11-06", AESEV: "MILD", AESER: false, AEREL: "RELATED", STATUS: "VERIFIED" },
                { recordId: "REC-3003", SUBJID: "SUBJ-101", AETERM: "Fatigue transient", AESTDTC: "2026-10-25", AESEV: "MODERATE", AESER: false, AEREL: "UNRELATED", STATUS: "VERIFIED" }
            ],
            LB: [
                { recordId: "REC-4001", SUBJID: "SUBJ-101", LBTEST: "HEMOGLOBIN", LBORRES: 14.2, LBORRESU: "g/dL", LBNRIND: "NORMAL", STATUS: "VERIFIED" },
                { recordId: "REC-4002", SUBJID: "SUBJ-102", LBTEST: "POTASSIUM", LBORRES: 4.1, LBORRESU: "mmol/L", LBNRIND: "NORMAL", STATUS: "VERIFIED" },
                { recordId: "REC-4003", SUBJID: "SUBJ-103", LBTEST: "ALT", LBORRES: 32.0, LBORRESU: "U/L", LBNRIND: "NORMAL", STATUS: "VERIFIED" },
                { recordId: "REC-4004", SUBJID: "SUBJ-104", LBTEST: "CREATININE", LBORRES: 1.05, LBORRESU: "mg/dL", LBNRIND: "NORMAL", STATUS: "VERIFIED" }
            ]
        },
        queries: [
            {
                queryId: "QRY-001",
                studyKey: "STUDY-101",
                formId: "VS",
                fieldId: "DIABP",
                subjectId: "SUBJ-104",
                status: "OPEN",
                severity: "HIGH",
                description: "Diastolic blood pressure (92 mmHg) exceeds normal baseline threshold for screening protocol.",
                createdDate: "2026-11-05T15:30:00Z",
                assignedTo: "site_coordinator_101"
            },
            {
                queryId: "QRY-002",
                studyKey: "STUDY-101",
                formId: "VS",
                fieldId: "SYSBP",
                subjectId: "SUBJ-102",
                status: "RESOLVED",
                severity: "MEDIUM",
                description: "Repeat systolic reading confirmed after 10-minute rest period.",
                createdDate: "2026-10-12T11:00:00Z",
                assignedTo: "site_coordinator_101"
            },
            {
                queryId: "QRY-003",
                studyKey: "STUDY-202",
                formId: "AE",
                fieldId: "AESER",
                subjectId: "SUBJ-208",
                status: "UNDER_REVIEW",
                severity: "CRITICAL",
                description: "Verify SAE criteria and expedited regulatory timeline classification.",
                createdDate: "2026-11-10T08:45:00Z",
                assignedTo: "safety_officer_global"
            },
            {
                queryId: "QRY-004",
                studyKey: "STUDY-101",
                formId: "LB",
                fieldId: "LBORRES",
                subjectId: "SUBJ-103",
                status: "CLOSED",
                severity: "LOW",
                description: "Confirmed unit mismatch corrected from SI to conventional notation.",
                createdDate: "2026-11-03T16:20:00Z",
                assignedTo: "data_manager_lead"
            }
        ],
        audits: [
            {
                eventId: "EVT-8801",
                timestamp: "2026-11-15T18:22:10Z",
                user: "c_miller_dm",
                role: "Clinical Data Manager",
                action: "RECORD_SUBMIT",
                target: "STUDY-101/VS/REC-1001",
                hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                wcagCompliance: "PASS",
                status: "VERIFIED"
            },
            {
                eventId: "EVT-8802",
                timestamp: "2026-11-15T18:35:45Z",
                user: "dr_watson_pi",
                role: "Principal Investigator",
                action: "ELECTRONIC_SIGNATURE",
                target: "STUDY-101/VS/REC-1001",
                hash: "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4",
                wcagCompliance: "PASS",
                status: "VERIFIED"
            },
            {
                eventId: "EVT-8803",
                timestamp: "2026-11-15T19:01:12Z",
                user: "sys_sync_worker",
                role: "Automated Pipeline",
                action: "DATA_EXPORT_DUCKDB",
                target: "STUDY-101/PARQUET",
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
                notes: "High contrast mode (7:1), ARIA labels and tabular fallback"
            },
            {
                criteria: "Principle 2: Operable",
                standard: "WCAG 2.1 Level AA",
                status: "PASS",
                wcagCompliance: "PASS",
                notes: "Keyboard navigation, focus indicators, modal focus management"
            },
            {
                criteria: "Principle 3: Understandable",
                standard: "WCAG 2.1 Level AA",
                status: "PASS",
                wcagCompliance: "PASS",
                notes: "Consistent navigation, descriptive error prompts, clear field labels"
            },
            {
                criteria: "Principle 4: Robust",
                standard: "WCAG 2.1 Level AA",
                status: "PASS",
                wcagCompliance: "PASS",
                notes: "Valid HTML5, standard ARIA roles, assistive technology compatible"
            }
        ]
    };

    // -------------------------------------------------------------------------
    // Code snippet templates
    // -------------------------------------------------------------------------
    const SNIPPETS = {
        sdk: {
            records: `# Extract clinical records into pandas DataFrame\nfrom imednet import ImednetClient\n\nclient = ImednetClient()\ndf = client.records.to_dataframe(\n    study_key="STUDY-101",\n    flatten=True\n)\nprint(df.head())`,
            studies: `# Query active clinical trials\nfrom imednet import ImednetClient\n\nclient = ImednetClient()\nstudies = client.studies.list(status="ACTIVE")\nfor s in studies:\n    print(f"{s.study_key}: {s.study_name} ({s.study_type})")`,
            forms: `# Fetch study Case Report Form (CRF) schemas\nfrom imednet import ImednetClient\n\nclient = ImednetClient()\nforms = client.forms.list(study_key="STUDY-101")\nfor f in forms:\n    print(f"{f.form_name} ({f.form_key})")`,
            queries: `# Monitor clinical queries and discrepancy flags\nfrom imednet import ImednetClient\n\nclient = ImednetClient()\nqueries = client.queries.list(\n    study_key="STUDY-101",\n    status="OPEN"\n)\nprint(f"Open queries: {len(queries)}")`
        },
        rest: {
            records: `GET /api/v1/edc/studies/STUDY-101/records?size=10 HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json`,
            studies: `GET /api/v1/edc/studies HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json`,
            forms: `GET /api/v1/edc/studies/STUDY-101/forms HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json`,
            queries: `GET /api/v1/edc/studies/STUDY-101/queries HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json`
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
            html += `<caption>${escapeHtml(captionText)} — ${items.length} records returned</caption>`;
        }
        html += `<thead><tr>`;
        html += `<th scope="col" class="index-col">#</th>`;
        keys.forEach(function (k) {
            html += `<th scope="col">${escapeHtml(k)}</th>`;
        });
        html += `</tr></thead><tbody>`;

        items.forEach(function (row, index) {
            html += `<tr>`;
            html += `<td class="index-col">${index}</td>`;
            keys.forEach(function (k) {
                const val = row[k];
                let cellHtml = escapeHtml(val);
                if (k === "STATUS" || k === "status" || k === "wcagCompliance") {
                    const badgeClass = String(val).toLowerCase().includes("verified") || String(val).toLowerCase().includes("pass") || String(val).toLowerCase().includes("active") || String(val).toLowerCase().includes("locked")
                        ? "badge-success"
                        : String(val).toLowerCase().includes("open") || String(val).toLowerCase().includes("critical")
                        ? "badge-warning"
                        : "badge-neutral";
                    cellHtml = `<span class="status-badge ${badgeClass}">${cellHtml}</span>`;
                } else if (typeof val === "boolean") {
                    cellHtml = val ? `<span class="status-badge badge-success">true</span>` : `<span class="status-badge badge-neutral">false</span>`;
                }
                html += `<td>${cellHtml}</td>`;
            });
            html += `</tr>`;
        });

        html += `</tbody></table></div>`;
        return html;
    }

    // -------------------------------------------------------------------------
    // Helper: Render Case Report Form (CRF) Structure
    // -------------------------------------------------------------------------
    function renderCrfStructure(formDef) {
        if (!formDef) return "";
        let html = `<div class="crf-view-container">`;
        html += `<div class="crf-header">`;
        html += `<h4><span class="crf-tag">${escapeHtml(formDef.domain)}</span> ${escapeHtml(formDef.formName)} <span class="crf-oid">(${escapeHtml(formDef.formId)})</span></h4>`;
        html += `<p class="crf-desc">${escapeHtml(formDef.description)}</p>`;
        html += `</div>`;

        html += `<div class="crf-fields-grid">`;
        formDef.fields.forEach(function (field) {
            html += `<div class="crf-field-card">`;
            html += `<div class="crf-field-header">`;
            html += `<span class="crf-field-id">${escapeHtml(field.id)}</span>`;
            if (field.required) {
                html += `<span class="crf-badge-required">Required</span>`;
            } else {
                html += `<span class="crf-badge-optional">Optional</span>`;
            }
            html += `</div>`;
            html += `<div class="crf-field-label">${escapeHtml(field.label)}</div>`;
            html += `<div class="crf-field-meta">`;
            html += `<span class="crf-meta-item"><strong>Type:</strong> <code>${escapeHtml(field.type)}</code></span>`;
            html += `<span class="crf-meta-item"><strong>Rule:</strong> <code>${escapeHtml(field.rule)}</code></span>`;
            html += `</div>`;
            html += `</div>`;
        });
        html += `</div></div>`;
        return html;
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

        personaTabs.forEach(function (tab) {
            tab.addEventListener("click", function () {
                const targetId = tab.getAttribute("data-target");
                switchPersona(targetId);
            });

            tab.addEventListener("keydown", function (e) {
                let index = Array.from(personaTabs).indexOf(tab);
                if (e.key === "ArrowRight" || e.key === "ArrowDown") {
                    e.preventDefault();
                    let next = personaTabs[(index + 1) % personaTabs.length];
                    next.focus();
                    next.click();
                } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
                    e.preventDefault();
                    let prev = personaTabs[(index - 1 + personaTabs.length) % personaTabs.length];
                    prev.focus();
                    prev.click();
                }
            });
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
        // 1. Integration Engineer Panel
        // ---------------------------------------------------------------------
        const ieSelectDataset = root.querySelector("#ie-dataset-select");
        const ieModeSdk = root.querySelector("#ie-mode-sdk");
        const ieModeRest = root.querySelector("#ie-mode-rest");
        const ieCodeInput = root.querySelector("#ie-code-input");
        const ieRunBtn = root.querySelector("#ie-run-btn");
        const ieMetricBadge = root.querySelector("#ie-metric-badge");
        const ieOutputTabTable = root.querySelector("#ie-tab-table");
        const ieOutputTabJson = root.querySelector("#ie-tab-json");
        const ieViewTable = root.querySelector("#ie-view-table");
        const ieViewJson = root.querySelector("#ie-view-json");
        const ieJsonPre = root.querySelector("#ie-json-pre code");
        const ieCopyBtn = root.querySelector("#ie-copy-btn");

        let currentIeMode = "sdk";

        function updateIeSnippet() {
            if (!ieSelectDataset || !ieCodeInput) return;
            const ds = ieSelectDataset.value;
            const snippet = SNIPPETS[currentIeMode][ds] || "";
            ieCodeInput.value = snippet;
        }

        if (ieSelectDataset) {
            ieSelectDataset.addEventListener("change", updateIeSnippet);
        }

        if (ieModeSdk && ieModeRest) {
            ieModeSdk.addEventListener("click", function () {
                currentIeMode = "sdk";
                ieModeSdk.classList.add("active");
                ieModeSdk.setAttribute("aria-pressed", "true");
                ieModeRest.classList.remove("active");
                ieModeRest.setAttribute("aria-pressed", "false");
                updateIeSnippet();
            });
            ieModeRest.addEventListener("click", function () {
                currentIeMode = "rest";
                ieModeRest.classList.add("active");
                ieModeRest.setAttribute("aria-pressed", "true");
                ieModeSdk.classList.remove("active");
                ieModeSdk.setAttribute("aria-pressed", "false");
                updateIeSnippet();
            });
        }

        function executeIntegrationQuery() {
            const t0 = performance.now();
            const datasetKey = ieSelectDataset ? ieSelectDataset.value : "records";
            let data = [];
            let caption = "Clinical Records DataFrame";

            if (datasetKey === "records") {
                data = MOCK_DATA.records.VS;
                caption = "pandas.DataFrame: STUDY-101 / VS (Vital Signs)";
            } else if (datasetKey === "studies") {
                data = MOCK_DATA.studies;
                caption = "Active Studies Catalog";
            } else if (datasetKey === "forms") {
                data = Object.keys(MOCK_DATA.forms).map(function (k) {
                    const f = MOCK_DATA.forms[k];
                    return {
                        formId: f.formId,
                        formName: f.formName,
                        domain: f.domain,
                        fieldCount: f.fields.length,
                        description: f.description
                    };
                });
                caption = "Study Forms Definition Catalog";
            } else if (datasetKey === "queries") {
                data = MOCK_DATA.queries;
                caption = "Clinical Query Ledger";
            }

            const elapsed = (performance.now() - t0 + Math.random() * 2 + 1.2).toFixed(1);

            if (ieMetricBadge) {
                ieMetricBadge.textContent = `⚡ Executed in ${elapsed} ms (Mock Client Engine) | Status: 200 OK`;
                ieMetricBadge.classList.add("active");
            }

            // Render Table
            if (ieViewTable) {
                ieViewTable.innerHTML = renderDataFrameTable(data, caption);
            }

            // Render JSON
            if (ieJsonPre) {
                ieJsonPre.textContent = JSON.stringify(data, null, 2);
            }
        }

        if (ieRunBtn) {
            ieRunBtn.addEventListener("click", executeIntegrationQuery);
        }

        // View toggle for IE
        function switchIeView(view) {
            if (view === "table") {
                ieOutputTabTable.classList.add("active");
                ieOutputTabTable.setAttribute("aria-selected", "true");
                ieOutputTabJson.classList.remove("active");
                ieOutputTabJson.setAttribute("aria-selected", "false");
                ieViewTable.removeAttribute("hidden");
                ieViewJson.setAttribute("hidden", "true");
            } else {
                ieOutputTabJson.classList.add("active");
                ieOutputTabJson.setAttribute("aria-selected", "true");
                ieOutputTabTable.classList.remove("active");
                ieOutputTabTable.setAttribute("aria-selected", "false");
                ieViewJson.removeAttribute("hidden");
                ieViewTable.setAttribute("hidden", "true");
            }
        }

        if (ieOutputTabTable && ieOutputTabJson) {
            ieOutputTabTable.addEventListener("click", function () { switchIeView("table"); });
            ieOutputTabJson.addEventListener("click", function () { switchIeView("json"); });
        }

        if (ieCopyBtn && ieJsonPre) {
            ieCopyBtn.addEventListener("click", function () {
                navigator.clipboard.writeText(ieJsonPre.textContent).then(function () {
                    const orig = ieCopyBtn.textContent;
                    ieCopyBtn.textContent = "Copied!";
                    setTimeout(function () { ieCopyBtn.textContent = orig; }, 1500);
                });
            });
        }

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
            const t0 = performance.now();
            const formKey = cdmSelectForm ? cdmSelectForm.value : "VS";
            const formDef = MOCK_DATA.forms[formKey] || MOCK_DATA.forms.VS;

            const elapsed = (performance.now() - t0 + Math.random() * 1.5 + 0.9).toFixed(1);

            if (cdmMetricBadge) {
                cdmMetricBadge.textContent = `✓ Schema Valid (CDISC ODM Compliant) — 0 Errors | Validated in ${elapsed} ms`;
                cdmMetricBadge.classList.add("active");
            }

            if (cdmViewCrf) {
                cdmViewCrf.innerHTML = renderCrfStructure(formDef);
            }

            if (cdmJsonPre) {
                cdmJsonPre.textContent = JSON.stringify(formDef, null, 2);
            }
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
                cdmOutputTabJson.classList.remove("active");
                cdmOutputTabJson.setAttribute("aria-selected", "false");
                cdmViewCrf.removeAttribute("hidden");
                cdmViewJson.setAttribute("hidden", "true");
            } else {
                cdmOutputTabJson.classList.add("active");
                cdmOutputTabJson.setAttribute("aria-selected", "true");
                cdmOutputTabCrf.classList.remove("active");
                cdmOutputTabCrf.setAttribute("aria-selected", "false");
                cdmViewJson.removeAttribute("hidden");
                cdmViewCrf.setAttribute("hidden", "true");
            }
        }

        if (cdmOutputTabCrf && cdmOutputTabJson) {
            cdmOutputTabCrf.addEventListener("click", function () { switchCdmView("crf"); });
            cdmOutputTabJson.addEventListener("click", function () { switchCdmView("json"); });
        }

        if (cdmCopyBtn && cdmJsonPre) {
            cdmCopyBtn.addEventListener("click", function () {
                navigator.clipboard.writeText(cdmJsonPre.textContent).then(function () {
                    const orig = cdmCopyBtn.textContent;
                    cdmCopyBtn.textContent = "Copied!";
                    setTimeout(function () { cdmCopyBtn.textContent = orig; }, 1500);
                });
            });
        }

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

            const t0 = performance.now();
            let data = [];
            let caption = "";
            let metricText = "";
            const elapsed = (performance.now() - t0 + Math.random() * 2 + 1.1).toFixed(1);

            if (scenario === "part11") {
                data = MOCK_DATA.audits;
                caption = "21 CFR Part 11 Audit Trail & Accessibility Log";
                metricText = `✓ 21 CFR Part 11 Electronic Signature & VPAT Check Verified | ${elapsed} ms`;
            } else if (scenario === "vpat") {
                data = MOCK_DATA.vpat;
                caption = "WCAG 2.1 AA Accessibility Conformance Verification";
                metricText = `✓ WCAG 2.1 AA Accessibility Conformance (VPAT) Verified | ${elapsed} ms`;
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
                audOutputTabJson.classList.remove("active");
                audOutputTabJson.setAttribute("aria-selected", "false");
                audViewTable.removeAttribute("hidden");
                audViewJson.setAttribute("hidden", "true");
            } else {
                audOutputTabJson.classList.add("active");
                audOutputTabJson.setAttribute("aria-selected", "true");
                audOutputTabTable.classList.remove("active");
                audOutputTabTable.setAttribute("aria-selected", "false");
                audViewJson.removeAttribute("hidden");
                audViewTable.setAttribute("hidden", "true");
            }
        }

        if (audOutputTabTable && audOutputTabJson) {
            audOutputTabTable.addEventListener("click", function () { switchAudView("table"); });
            audOutputTabJson.addEventListener("click", function () { switchAudView("json"); });
        }

        if (audCopyBtn && audJsonPre) {
            audCopyBtn.addEventListener("click", function () {
                navigator.clipboard.writeText(audJsonPre.textContent).then(function () {
                    const orig = audCopyBtn.textContent;
                    audCopyBtn.textContent = "Copied!";
                    setTimeout(function () { audCopyBtn.textContent = orig; }, 1500);
                });
            });
        }

        // Run default executions on load so user sees immediately populated results
        updateIeSnippet();
        executeIntegrationQuery();
        executeCdmValidation();
        executeAuditorVerification();
    }

    // -------------------------------------------------------------------------
    // Keyboard focus management for overflowing preformatted code blocks
    // -------------------------------------------------------------------------
    function initAccessibleCodeBlocks() {
        function checkCodeBlocks() {
            const blocks = document.querySelectorAll("pre");
            blocks.forEach(function (block) {
                if (block.scrollWidth > block.clientWidth) {
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
        }

        checkCodeBlocks();
        window.addEventListener("resize", checkCodeBlocks);
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
