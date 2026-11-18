"""Automated regression tests for showcase URL hash routing and scenario selection."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest

from imednet.endpoints.records import RecordsEndpoint
from imednet.models.forms import Form
from imednet.models.queries import QueryStatus
from imednet.models.studies import Study


def get_repo_root() -> Path:
    """Return repository root directory."""
    return Path(__file__).resolve().parents[2]


def test_readme_persona_anchor_links() -> None:
    """Verify that README role links target distinct persona panel IDs instead of generic anchors."""
    repo_root = get_repo_root()
    readme_path = repo_root / "README.md"
    assert readme_path.exists(), "README.md must exist"

    content = readme_path.read_text(encoding="utf-8")

    # Match persona links
    ie_match = re.search(r"\[Integration Engineer Live Console\]\(([^)]+)\)", content)
    cdm_match = re.search(r"\[Clinical Data Manager Form Validator\]\(([^)]+)\)", content)
    aud_match = re.search(r"\[Regulatory Compliance Auditor Verification\]\(([^)]+)\)", content)

    assert ie_match, "Integration Engineer link not found in README.md"
    assert cdm_match, "Clinical Data Manager link not found in README.md"
    assert aud_match, "Regulatory Compliance Auditor link not found in README.md"

    ie_url = ie_match.group(1)
    cdm_url = cdm_match.group(1)
    aud_url = aud_match.group(1)

    expected_prefix = "https://fderuiter.github.io/imednet-toolkit/"
    assert ie_url.startswith(expected_prefix), (
        f"Expected canonical prefix {expected_prefix}, got {ie_url}"
    )
    assert cdm_url.startswith(expected_prefix), (
        f"Expected canonical prefix {expected_prefix}, got {cdm_url}"
    )
    assert aud_url.startswith(expected_prefix), (
        f"Expected canonical prefix {expected_prefix}, got {aud_url}"
    )

    assert ie_url.endswith("#panel-integration"), (
        f"Expected #panel-integration anchor, got {ie_url}"
    )
    assert cdm_url.endswith("#panel-datamanager"), (
        f"Expected #panel-datamanager anchor, got {cdm_url}"
    )
    assert aud_url.endswith("#panel-auditor"), f"Expected #panel-auditor anchor, got {aud_url}"

    # Ensure none of the persona links point to the generic showcase anchor
    for label, url in [
        ("Integration Engineer", ie_url),
        ("Clinical Data Manager", cdm_url),
        ("Regulatory Compliance Auditor", aud_url),
    ]:
        assert url.startswith(expected_prefix), (
            f"{label} link must start with canonical prefix {expected_prefix}"
        )
        assert not url.endswith("#interactive-showcase"), (
            f"{label} link must not target generic #interactive-showcase"
        )


def test_docs_markup_defines_matching_panels_and_scenario_select() -> None:
    """Verify that docs/index.rst defines panel IDs matching the README anchors and scenario selector."""
    repo_root = get_repo_root()
    index_rst = repo_root / "docs" / "index.rst"
    assert index_rst.exists(), "docs/index.rst must exist"

    content = index_rst.read_text(encoding="utf-8")

    # Panels referenced in README anchors
    assert 'id="panel-integration"' in content, "panel-integration missing in docs/index.rst"
    assert 'id="panel-datamanager"' in content, "panel-datamanager missing in docs/index.rst"
    assert 'id="panel-auditor"' in content, "panel-auditor missing in docs/index.rst"

    # Auditor scenario selector and expected options
    assert 'id="aud-scenario-select"' in content, "aud-scenario-select missing in docs/index.rst"
    assert 'value="part11"' in content, "part11 option missing in aud-scenario-select"
    assert 'value="vpat"' in content, "vpat option missing in aud-scenario-select"


def test_showcase_script_implements_hash_routing() -> None:
    """Verify that showcase.js reads URL hash, handles hashchange, and syncs history state."""
    repo_root = get_repo_root()
    script_path = repo_root / "docs" / "_static" / "showcase.js"
    assert script_path.exists(), "showcase.js must exist"

    content = script_path.read_text(encoding="utf-8")

    # Hash reading and hashchange listener
    assert "window.location.hash" in content, "showcase.js must inspect window.location.hash"
    assert 'addEventListener("hashchange"' in content, (
        "showcase.js must register a hashchange listener"
    )

    # history.replaceState synchronization in switchPersona
    assert "history.replaceState" in content, (
        "showcase.js must call history.replaceState for URL sync"
    )

    # Safe fallback to default panel-integration
    assert "panel-integration" in content, "showcase.js must reference default panel-integration"


def test_showcase_script_binds_scenario_selector_and_renders_vpat() -> None:
    """Verify that showcase.js binds #aud-scenario-select and provides VPAT mock data."""
    repo_root = get_repo_root()
    script_path = repo_root / "docs" / "_static" / "showcase.js"
    content = script_path.read_text(encoding="utf-8")

    # Select binding
    assert "#aud-scenario-select" in content, "showcase.js must query #aud-scenario-select"
    assert 'audScenarioSelect.addEventListener("change"' in content, (
        "showcase.js must attach change event listener to audScenarioSelect"
    )

    # VPAT mock data
    assert "vpat:" in content, "showcase.js must define vpat dataset in MOCK_DATA"
    assert "WCAG 2.1 Level AA" in content, "vpat dataset must reference WCAG 2.1 Level AA standard"

    # Unmapped scenario safety guard
    assert 'scenario !== "part11" && scenario !== "vpat"' in content, (
        "executeAuditorVerification must validate scenarios to preserve table content on unmapped scenarios"
    )


def test_showcase_sdk_snippets_syntax_and_api_contracts() -> None:
    """Verify that all showcase SDK code snippets parse as valid Python and conform to the public API contract."""
    repo_root = get_repo_root()
    script_path = repo_root / "docs" / "_static" / "showcase.js"
    assert script_path.exists(), "showcase.js must exist"

    content = script_path.read_text(encoding="utf-8")
    sdk_match = re.search(r"sdk:\s*\{(.*?)\n\s*\},?\s*rest:", content, re.DOTALL)
    assert sdk_match, "sdk snippet dictionary not found in showcase.js"

    snippets = dict(re.findall(r"(\w+):\s*`([^`]+)`", sdk_match.group(1)))
    expected_snippets = {"records", "studies", "forms", "queries"}
    assert set(snippets.keys()) == expected_snippets, (
        f"Expected snippets {expected_snippets}, found {set(snippets.keys())}"
    )

    class SnippetInspector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []
            self.attributes: list[tuple[str, str]] = []

        def visit_Call(self, node: ast.Call) -> None:
            func_name = ast.unparse(node.func)
            kw = {k.arg: ast.unparse(k.value) for k in node.keywords if k.arg is not None}
            self.calls.append((func_name, kw))
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            self.attributes.append((ast.unparse(node.value), node.attr))
            self.generic_visit(node)

    trees: dict[str, ast.AST] = {}
    inspectors: dict[str, SnippetInspector] = {}

    for name, raw_snippet in snippets.items():
        # JavaScript template literal has escaped newlines
        code_py = raw_snippet.replace(r"\n", "\n")
        # Validate snippet parses cleanly with ast.parse
        tree = ast.parse(code_py)
        trees[name] = tree
        inspector = SnippetInspector()
        inspector.visit(tree)
        inspectors[name] = inspector

    # 1. Records extraction snippet
    records_insp = inspectors["records"]
    assert "to_df" not in snippets["records"], "records snippet must not call nonexistent to_df"
    records_calls = [
        (fn, kw) for fn, kw in records_insp.calls if fn == "client.records.to_dataframe"
    ]
    assert records_calls, "records snippet must call client.records.to_dataframe"
    _, records_kw = records_calls[0]
    assert records_kw.get("study_key") == "'STUDY-101'", (
        f"Expected study_key='STUDY-101', got {records_kw.get('study_key')}"
    )
    assert records_kw.get("flatten") == "True", (
        f"Expected flatten=True, got {records_kw.get('flatten')}"
    )
    assert hasattr(RecordsEndpoint, "to_dataframe"), (
        "RecordsEndpoint must define to_dataframe method"
    )

    # 2. Studies snippet
    studies_insp = inspectors["studies"]
    studies_calls = [(fn, kw) for fn, kw in studies_insp.calls if fn == "client.studies.list"]
    assert studies_calls, "studies snippet must call client.studies.list"
    studies_item_attrs = {attr for val, attr in studies_insp.attributes if val == "s"}
    assert "study_name" in studies_item_attrs, "studies snippet must access s.study_name"
    assert "study_type" in studies_item_attrs, "studies snippet must access s.study_type"
    for invalid_attr in ("name", "enrolled_subjects", "target_accrual"):
        assert invalid_attr not in studies_item_attrs, (
            f"studies snippet must not access invalid attribute {invalid_attr}"
        )
    for attr in studies_item_attrs:
        assert attr in Study.model_fields, f"Attribute '{attr}' not found on Study model"

    # 3. Forms snippet
    forms_insp = inspectors["forms"]
    forms_calls = [(fn, kw) for fn, kw in forms_insp.calls if fn == "client.forms.list"]
    assert forms_calls, "forms snippet must call client.forms.list"
    forms_item_attrs = {attr for val, attr in forms_insp.attributes if val == "f"}
    assert "form_name" in forms_item_attrs, "forms snippet must access f.form_name"
    assert "form_key" in forms_item_attrs, "forms snippet must access f.form_key"
    assert "fields" not in forms_item_attrs, "forms snippet must not access nonexistent fields"
    for attr in forms_item_attrs:
        assert attr in Form.model_fields, f"Attribute '{attr}' not found on Form model"

    # 4. Queries snippet
    queries_insp = inspectors["queries"]
    queries_calls = [(fn, kw) for fn, kw in queries_insp.calls if fn == "client.queries.list"]
    assert queries_calls, "queries snippet must call client.queries.list"
    _, queries_kw = queries_calls[0]
    assert queries_kw.get("status") == "'OPEN'", (
        f"Expected queries status='OPEN', got {queries_kw.get('status')}"
    )
    assert "severity" not in queries_kw, "queries snippet must not filter by severity"
    assert QueryStatus.OPEN.value == "OPEN", "OPEN must be a valid QueryStatus lifecycle state"


def parse_rest_request_line(
    raw_snippet: str,
) -> tuple[str, str, dict[str, list[str]], str]:
    """Parse HTTP request line from raw snippet into (method, path, query_params, http_version)."""
    normalized = raw_snippet.replace(r"\n", "\n").strip()
    first_line = normalized.splitlines()[0]
    parts = first_line.split(" ")
    assert len(parts) == 3, (
        f"Expected request line 'METHOD TARGET HTTP/VERSION', got: {first_line!r}"
    )
    method, target, http_version = parts
    parsed_target = urllib.parse.urlsplit(target)
    query_params = urllib.parse.parse_qs(parsed_target.query, keep_blank_values=True)
    return method, parsed_target.path, query_params, http_version


def validate_rest_query_parameters(
    raw_snippet: str,
    allowed_query_keys: set[str],
) -> tuple[str, str, dict[str, list[str]]]:
    """Validate that query parameters in the request target conform to the allowed specification."""
    method, path, query_params, http_version = parse_rest_request_line(raw_snippet)
    assert method == "GET", f"Expected GET method, got {method}"
    assert http_version == "HTTP/1.1", f"Expected HTTP/1.1, got {http_version}"

    disallowed_keys = set(query_params.keys()) - allowed_query_keys
    assert not disallowed_keys, (
        f"Endpoint '{path}' contains unsupported query parameters: {sorted(disallowed_keys)}. "
        f"Allowed parameters are: {sorted(allowed_query_keys)}"
    )
    assert "status" not in query_params, (
        f"Endpoint '{path}' must not include unsupported standalone 'status' query parameter."
    )
    return method, path, query_params


def test_showcase_rest_snippets_syntax_and_api_contracts() -> None:
    """Verify that all showcase REST snippets match the official EDC API contracts."""
    repo_root = get_repo_root()
    script_path = repo_root / "docs" / "_static" / "showcase.js"
    assert script_path.exists(), "showcase.js must exist"

    content = script_path.read_text(encoding="utf-8")
    rest_match = re.search(r"rest:\s*\{([^}]+)\}", content)
    assert rest_match, "rest snippet dictionary not found in showcase.js"

    snippets = dict(re.findall(r"(\w+):\s*`([^`]+)`", rest_match.group(1)))
    expected_snippets = {"records", "studies", "forms", "queries"}
    assert set(snippets.keys()) == expected_snippets, (
        f"Expected snippets {expected_snippets}, found {set(snippets.keys())}"
    )

    required_headers = [
        "Host: edc.prod.imednetapi.com",
        "x-api-key: <API_KEY>",
        "x-imn-security-key: <SECURITY_KEY>",
        "Accept: application/json",
    ]

    allowed_query_keys_by_endpoint: dict[str, set[str]] = {
        "records": {"page", "size", "sort", "filter", "recordDataFilter"},
        "studies": {"page", "size", "sort", "filter"},
        "forms": {"page", "size", "sort", "filter"},
        "queries": {"page", "size", "sort", "filter"},
    }

    parsed_requests: dict[str, tuple[str, str, dict[str, list[str]]]] = {}

    for name, raw_snippet in snippets.items():
        # Validate required headers are present
        for header in required_headers:
            assert header in raw_snippet, f"REST snippet '{name}' must include header '{header}'"

        # Validate obsolete or fictional patterns are not present
        assert "api.imednet.example.org" not in raw_snippet, (
            f"REST snippet '{name}' must not reference fictional domain api.imednet.example.org"
        )
        assert "Authorization: Bearer" not in raw_snippet, (
            f"REST snippet '{name}' must use EDC api-key security headers instead of Bearer token"
        )

        # Validate endpoint path prefix
        assert "GET /api/v1/edc/" in raw_snippet, (
            f"REST snippet '{name}' endpoint path must be prefixed with /api/v1/edc/"
        )

        # Validate parsed query keys against endpoint specification
        method, path, query_params = validate_rest_query_parameters(
            raw_snippet,
            allowed_query_keys=allowed_query_keys_by_endpoint[name],
        )
        parsed_requests[name] = (method, path, query_params)

    # 1. Records endpoint and query parameter assertions
    method, path, query_params = parsed_requests["records"]
    assert method == "GET"
    assert path == "/api/v1/edc/studies/STUDY-101/records"
    assert query_params == {"size": ["10"]}
    assert "limit" not in query_params, "records snippet must use size instead of limit"
    assert "form" not in query_params, "records snippet must not filter by invalid form param"
    assert "status" not in query_params

    # 2. Studies endpoint assertions
    method, path, query_params = parsed_requests["studies"]
    assert method == "GET"
    assert path == "/api/v1/edc/studies"
    assert "status" not in query_params, (
        "studies snippet must not include unsupported status parameter"
    )
    assert not query_params, f"studies snippet expected no query parameters, got {query_params}"

    # 3. Forms endpoint assertions
    method, path, query_params = parsed_requests["forms"]
    assert method == "GET"
    assert path == "/api/v1/edc/studies/STUDY-101/forms"
    assert not query_params, f"forms snippet expected no query parameters, got {query_params}"

    # 4. Queries endpoint assertions
    method, path, query_params = parsed_requests["queries"]
    assert method == "GET"
    assert path == "/api/v1/edc/studies/STUDY-101/queries"
    assert "status" not in query_params, (
        "queries snippet must not include unsupported status parameter"
    )
    assert not query_params, f"queries snippet expected no query parameters, got {query_params}"


@pytest.mark.parametrize(
    ("invalid_snippet", "allowed_keys", "expected_error_substring"),
    [
        (
            "GET /api/v1/edc/studies?status=ACTIVE HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json",
            {"page", "size", "sort", "filter"},
            "unsupported query parameters: ['status']",
        ),
        (
            "GET /api/v1/edc/studies/STUDY-101/queries?status=OPEN HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json",
            {"page", "size", "sort", "filter"},
            "unsupported query parameters: ['status']",
        ),
        (
            "GET /api/v1/edc/studies/STUDY-101/records?limit=10&form=VS HTTP/1.1\nHost: edc.prod.imednetapi.com\nx-api-key: <API_KEY>\nx-imn-security-key: <SECURITY_KEY>\nAccept: application/json",
            {"page", "size", "sort", "filter", "recordDataFilter"},
            "unsupported query parameters",
        ),
    ],
)
def test_showcase_rest_snippets_rejects_unsupported_query_parameters(
    invalid_snippet: str,
    allowed_keys: set[str],
    expected_error_substring: str,
) -> None:
    """Verify that REST snippet validation rejects unsupported query parameters like status or limit."""
    with pytest.raises(AssertionError, match=re.escape(expected_error_substring)):
        validate_rest_query_parameters(invalid_snippet, allowed_query_keys=allowed_keys)


def test_showcase_accessible_code_blocks_module_scope_and_bindings() -> None:
    """Verify that checkCodeBlocks is in module scope, called on tab reveal, and observes attribute mutations."""
    repo_root = get_repo_root()
    script_path = repo_root / "docs" / "_static" / "showcase.js"
    assert script_path.exists(), "showcase.js must exist"

    content = script_path.read_text(encoding="utf-8")

    # 1. checkCodeBlocks and scheduleCheckCodeBlocks lifted to shared module scope (defined before initExplorer)
    check_pos = content.find("function checkCodeBlocks()")
    schedule_pos = content.find("function scheduleCheckCodeBlocks()")
    init_explorer_pos = content.find("function initExplorer()")
    init_accessible_pos = content.find("function initAccessibleCodeBlocks()")

    assert check_pos != -1, "checkCodeBlocks function must be defined"
    assert schedule_pos != -1, "scheduleCheckCodeBlocks function must be defined"
    assert init_explorer_pos != -1, "initExplorer function must be defined"
    assert init_accessible_pos != -1, "initAccessibleCodeBlocks function must be defined"

    # Must be at module scope, preceding initExplorer and outside initAccessibleCodeBlocks
    assert check_pos < init_explorer_pos, (
        "checkCodeBlocks must be lifted to module scope before initExplorer"
    )
    assert schedule_pos < init_explorer_pos, (
        "scheduleCheckCodeBlocks must be lifted to module scope before initExplorer"
    )

    # 2. Scheduling debouncing via requestAnimationFrame and cancelAnimationFrame
    assert "requestAnimationFrame" in content, (
        "showcase.js must use requestAnimationFrame for check scheduling"
    )
    assert "cancelAnimationFrame" in content, (
        "showcase.js must use cancelAnimationFrame to debounce frame requests"
    )

    # 3. Tab switching functions call scheduleCheckCodeBlocks
    for view_func in ["switchPersona", "switchIeView", "switchCdmView", "switchAudView"]:
        pattern = rf"function\s+{view_func}\b.*?scheduleCheckCodeBlocks"
        assert re.search(pattern, content, re.DOTALL), (
            f"{view_func} must invoke scheduleCheckCodeBlocks on visibility change"
        )

    # 4. JSON rendering execution functions call scheduleCheckCodeBlocks
    for exec_func in [
        "executeIntegrationQuery",
        "executeCdmValidation",
        "executeAuditorVerification",
    ]:
        pattern = rf"function\s+{exec_func}\b.*?scheduleCheckCodeBlocks"
        assert re.search(pattern, content, re.DOTALL), (
            f"{exec_func} must invoke scheduleCheckCodeBlocks after rendering dynamic JSON content"
        )

    # 5. MutationObserver fallback observing hidden and class attributes
    assert "MutationObserver" in content, "initAccessibleCodeBlocks must instantiate MutationObserver"
    assert 'attributeFilter: ["hidden", "class"]' in content or '["hidden", "class"]' in content, (
        "MutationObserver must observe hidden and class attributes"
    )
    assert "subtree: true" in content, "MutationObserver must observe subtree mutations"

    # 6. WCAG 2.1 AA attributes and zero-dimension safety check
    assert 'tabindex", "0"' in content or "tabindex', '0'" in content, (
        "checkCodeBlocks must set tabindex=0 on overflowing blocks"
    )
    assert 'role", "region"' in content or "role', 'region'" in content, (
        "checkCodeBlocks must set role=region on overflowing blocks"
    )
    assert 'aria-label", "Code snippet"' in content or "aria-label', 'Code snippet'" in content, (
        "checkCodeBlocks must set aria-label='Code snippet' on overflowing blocks"
    )
    assert "block.clientWidth > 0" in content, (
        "checkCodeBlocks must ensure clientWidth > 0 to avoid setting tabindex on hidden containers"
    )
    assert "removeAttribute" in content and "tabindex" in content, (
        "checkCodeBlocks must remove stale tabindex when block is not overflowing or hidden"
    )


def test_showcase_code_block_overflow_evaluation_lifecycle() -> None:
    """Verify code block accessibility lifecycle (hidden container, reveal, hide, and debounce) via Node.js."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node runtime not available for DOM mock evaluation")

    repo_root = get_repo_root()
    script_path = repo_root / "docs" / "_static" / "showcase.js"

    js_test = f"""
    const fs = require('fs');
    const code = fs.readFileSync('{script_path.as_posix()}', 'utf8');

    let rafCallbacks = [];
    let rafCancelCount = 0;
    let nextRafId = 1;

    global.requestAnimationFrame = (cb) => {{
        const id = nextRafId++;
        rafCallbacks.push({{ id, cb }});
        return id;
    }};

    global.cancelAnimationFrame = (id) => {{
        rafCancelCount++;
        rafCallbacks = rafCallbacks.filter(item => item.id !== id);
    }};

    let listeners = {{}};
    global.window = {{
        addEventListener: (event, cb) => {{
            listeners[event] = listeners[event] || [];
            listeners[event].push(cb);
        }},
        history: {{ replaceState: () => {{}} }},
        location: {{ hash: '' }}
    }};

    function makeElement(id, tagName = 'div') {{
        const attrs = {{}};
        const classes = new Set();
        return {{
            id,
            tagName,
            clientWidth: 0,
            scrollWidth: 0,
            attrs,
            classList: {{
                add: (...cls) => cls.forEach(c => classes.add(c)),
                remove: (...cls) => cls.forEach(c => classes.delete(c)),
                toggle: (cls, force) => {{
                    if (force === undefined) force = !classes.has(cls);
                    if (force) classes.add(cls); else classes.delete(cls);
                    return force;
                }},
                contains: (cls) => classes.has(cls)
            }},
            setAttribute: (k, v) => {{ attrs[k] = String(v); }},
            removeAttribute: (k) => {{ delete attrs[k]; }},
            getAttribute: (k) => attrs[k] || null,
            hasAttribute: (k) => k in attrs,
            addEventListener: () => {{}}
        }};
    }}

    const preOverflow = makeElement('cdm-json-pre', 'pre');
    preOverflow.clientWidth = 0;
    preOverflow.scrollWidth = 0;

    const preNormal = makeElement('normal-pre', 'pre');
    preNormal.clientWidth = 500;
    preNormal.scrollWidth = 400;

    const allPre = [preOverflow, preNormal];

    global.document = {{
        readyState: 'complete',
        getElementById: () => null,
        querySelectorAll: (sel) => sel === 'pre' ? allPre : [],
        addEventListener: () => {{}},
        body: makeElement('body')
    }};

    global.MutationObserver = class {{
        constructor(cb) {{ this.cb = cb; }}
        observe() {{}}
        disconnect() {{}}
    }};

    eval(code);

    function flushRaf() {{
        const cbs = rafCallbacks.slice();
        rafCallbacks = [];
        cbs.forEach(item => item.cb());
    }}

    const results = {{}};

    // 1. Initial load (container hidden)
    results.initialTabindex = preOverflow.getAttribute('tabindex');

    // 2. Container revealed and overflowing
    preOverflow.clientWidth = 400;
    preOverflow.scrollWidth = 800;
    listeners['resize'][0]();
    flushRaf();
    results.revealedTabindex = preOverflow.getAttribute('tabindex');
    results.revealedRole = preOverflow.getAttribute('role');
    results.revealedAriaLabel = preOverflow.getAttribute('aria-label');

    // 3. Normal block should not receive tabindex
    results.normalTabindex = preNormal.getAttribute('tabindex');

    // 4. Container hidden again (clientWidth becomes 0)
    preOverflow.clientWidth = 0;
    preOverflow.scrollWidth = 0;
    listeners['resize'][0]();
    flushRaf();
    results.hiddenAgainTabindex = preOverflow.getAttribute('tabindex');
    results.hiddenAgainRole = preOverflow.getAttribute('role');

    // 5. Verify debouncing
    listeners['resize'][0]();
    listeners['resize'][0]();
    listeners['resize'][0]();
    results.canceledRafCount = rafCancelCount;

    console.log(JSON.stringify(results));
    """

    res = subprocess.run(
        [node_bin, "-e", js_test],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(res.stdout)

    # 1. Hidden block initially has no tabindex
    assert data["initialTabindex"] is None, "Hidden code block must not have tabindex initially"

    # 2. Revealed overflowing block receives WCAG Level AA accessibility attributes
    assert data["revealedTabindex"] == "0", "Overflowing block must receive tabindex='0'"
    assert data["revealedRole"] == "region", "Overflowing block must receive role='region'"
    assert data["revealedAriaLabel"] == "Code snippet", (
        "Overflowing block must receive aria-label='Code snippet'"
    )

    # 3. Non-overflowing block retains default behavior without tabindex
    assert data["normalTabindex"] is None, "Non-overflowing block must not have tabindex"

    # 4. Hidden again removes stale tabindex and role attributes
    assert data["hiddenAgainTabindex"] is None, "Hidden block must clean up stale tabindex"
    assert data["hiddenAgainRole"] is None, "Hidden block must clean up role"

    # 5. Rapid sequential triggers debounce frame requests
    assert data["canceledRafCount"] >= 2, "Rapid triggers must cancel previous animation frames"

