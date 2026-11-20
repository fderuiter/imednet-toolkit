"""Automated regression tests for showcase URL hash routing and scenario selection."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import shutil
import subprocess
import types
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest
from scripts.validate_showcases import (
    validate_canonical_showcase_terminology,
    validate_landing_showcase,
)

from imednet.endpoints.records import RecordsEndpoint
from imednet.models.forms import Form
from imednet.models.queries import Query, QueryStatus
from imednet.models.records import Record
from imednet.models.studies import Study
from imednet.simulation.state import get_default_simulation_state


def get_repo_root() -> Path:
    """Return repository root directory."""
    return Path(__file__).resolve().parents[2]


def test_readme_persona_anchor_links() -> None:
    """Verify that README role links target distinct persona panel IDs instead of generic anchors."""
    repo_root = get_repo_root()
    readme_path = repo_root / "README.md"
    assert readme_path.exists(), "README.md must exist"

    content = readme_path.read_text(encoding="utf-8")

    # Verify CI badge targets canonical main.yml pipeline and preserves branch query
    ci_match = re.search(
        r"\[!\[CI\]\((https://img\.shields\.io/github/actions/workflow/status/[^)]+)\)\]\((https://github\.com/[^)]+)\)",
        content,
    )
    assert ci_match, "CI badge link not found in README.md"
    ci_img, ci_target = ci_match.groups()
    assert "main.yml?branch=main" in ci_img, (
        f"Expected CI badge image to target main.yml with branch=main, got {ci_img}"
    )
    assert ci_target == "https://github.com/fderuiter/imednet-toolkit/actions/workflows/main.yml", (
        f"Expected CI badge destination to point to main.yml, got {ci_target}"
    )
    assert "ci.yml" not in content, "README.md must not reference obsolete ci.yml workflow"

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


def test_readme_ci_badge_target() -> None:
    """Verify that README CI badge points to canonical main.yml workflow and preserves branch query."""
    repo_root = get_repo_root()
    readme_path = repo_root / "README.md"
    assert readme_path.exists(), "README.md must exist"

    content = readme_path.read_text(encoding="utf-8")
    assert "ci.yml" not in content, "README.md must not reference obsolete ci.yml workflow"

    ci_match = re.search(
        r"\[!\[CI\]\((https://img\.shields\.io/github/actions/workflow/status/[^)]+)\)\]\((https://github\.com/[^)]+)\)",
        content,
    )
    assert ci_match, "CI badge link not found in README.md"
    ci_img, ci_target = ci_match.groups()
    assert "main.yml?branch=main" in ci_img, (
        f"Expected CI badge image to target main.yml with branch=main, got {ci_img}"
    )
    assert ci_target == "https://github.com/fderuiter/imednet-toolkit/actions/workflows/main.yml", (
        f"Expected CI badge destination to point to main.yml, got {ci_target}"
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
    assert "ImednetClient" not in snippets["records"], "records snippet must use ImednetSDK"
    sdk_init_calls = [(fn, kw) for fn, kw in records_insp.calls if fn == "ImednetSDK"]
    assert sdk_init_calls and sdk_init_calls[0][1].get("simulation_mode") == "True", (
        "records snippet must instantiate ImednetSDK(simulation_mode=True)"
    )
    records_list_calls = [(fn, kw) for fn, kw in records_insp.calls if fn == "sdk.records.list"]
    assert records_list_calls and records_list_calls[0][1].get("study_key") == "'STUDY'", (
        "records snippet must materialize sdk.records.list(study_key='STUDY')"
    )
    records_calls = [(fn, kw) for fn, kw in records_insp.calls if fn == "sdk.records.to_dataframe"]
    assert records_calls, "records snippet must call sdk.records.to_dataframe"
    _, records_kw = records_calls[0]
    assert records_kw.get("study_key") == "'STUDY'", (
        f"Expected study_key='STUDY', got {records_kw.get('study_key')}"
    )
    assert records_kw.get("flatten") == "True", (
        f"Expected flatten=True, got {records_kw.get('flatten')}"
    )
    assert hasattr(RecordsEndpoint, "to_dataframe"), (
        "RecordsEndpoint must define to_dataframe method"
    )

    # 2. Studies snippet
    studies_insp = inspectors["studies"]
    studies_calls = [(fn, kw) for fn, kw in studies_insp.calls if fn == "sdk.studies.list"]
    assert studies_calls, "studies snippet must call sdk.studies.list"
    _, studies_kw = studies_calls[0]
    assert "status" not in studies_kw, "studies.list() must not pass unsupported status filter"
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
    forms_calls = [(fn, kw) for fn, kw in forms_insp.calls if fn == "sdk.forms.list"]
    assert forms_calls, "forms snippet must call sdk.forms.list"
    _, forms_kw = forms_calls[0]
    assert forms_kw.get("study_key") == "'STUDY'", (
        f"Expected study_key='STUDY', got {forms_kw.get('study_key')}"
    )
    forms_item_attrs = {attr for val, attr in forms_insp.attributes if val == "f"}
    assert "form_name" in forms_item_attrs, "forms snippet must access f.form_name"
    assert "form_key" in forms_item_attrs, "forms snippet must access f.form_key"
    assert "fields" not in forms_item_attrs, "forms snippet must not access nonexistent fields"
    for attr in forms_item_attrs:
        assert attr in Form.model_fields, f"Attribute '{attr}' not found on Form model"

    # 4. Queries snippet
    queries_insp = inspectors["queries"]
    queries_calls = [(fn, kw) for fn, kw in queries_insp.calls if fn == "sdk.queries.list"]
    assert queries_calls, "queries snippet must call sdk.queries.list"
    _, queries_kw = queries_calls[0]
    assert queries_kw.get("study_key") == "'STUDY'", (
        f"Expected study_key='STUDY', got {queries_kw.get('study_key')}"
    )
    assert "status" not in queries_kw, (
        "sdk.queries.list() must not pass unsupported status filter keyword"
    )
    assert "severity" not in queries_kw, "queries snippet must not filter by severity"
    assert ("QueryStatus", "OPEN") in queries_insp.attributes, (
        "queries snippet must filter materialized queries using QueryStatus.OPEN"
    )
    assert QueryStatus.OPEN.value == "OPEN", "OPEN must be a valid QueryStatus lifecycle state"

    # 5. Execute all four snippets offline against seeded STUDY simulation fixtures
    expected_models: dict[str, type[object]] = {
        "records": Record,
        "studies": Study,
        "forms": Form,
        "queries": Query,
    }
    for name, tree in trees.items():
        get_default_simulation_state().reset()
        namespace: dict[str, object] = {"__name__": "__test_showcase__"}
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(tree, filename=f"showcase.js:{name}", mode="exec"), namespace)  # noqa: S102

        for var_name, var_val in namespace.items():
            assert not isinstance(var_val, (types.GeneratorType, Iterator)), (
                f"Snippet '{name}' left unmaterialized lazy iterator in '{var_name}'"
            )

        items = namespace.get(name)
        expected_cls = expected_models[name]
        assert isinstance(items, list) and len(items) > 0, (
            f"Snippet '{name}' must materialize non-empty list of {expected_cls.__name__} fixtures"
        )
        assert all(isinstance(item, expected_cls) for item in items), (
            f"Snippet '{name}' items must all be {expected_cls.__name__} instances"
        )
    get_default_simulation_state().reset()


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
    assert path == "/api/v1/edc/studies/STUDY/records"
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
    assert path == "/api/v1/edc/studies/STUDY/forms"
    assert not query_params, f"forms snippet expected no query parameters, got {query_params}"

    # 4. Queries endpoint assertions
    method, path, query_params = parsed_requests["queries"]
    assert method == "GET"
    assert path == "/api/v1/edc/studies/STUDY/queries"
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
    assert "MutationObserver" in content, (
        "initAccessibleCodeBlocks must instantiate MutationObserver"
    )
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


def test_panel_integration_readonly_explorer_and_quick_start_callout() -> None:
    """Verify #panel-integration renders read-only viewer, copy control, Quick Start callout, and no mock editor."""
    repo_root = get_repo_root()
    index_content = (repo_root / "docs" / "index.rst").read_text(encoding="utf-8")
    readme_content = (repo_root / "README.md").read_text(encoding="utf-8")
    showcase_js = (repo_root / "docs" / "_static" / "showcase.js").read_text(encoding="utf-8")

    panel_start = index_content.index('id="panel-integration"')
    panel_end = index_content.index('id="panel-datamanager"')
    panel_integration = index_content[panel_start:panel_end]

    assert "<textarea" not in index_content, "Landing page must not render editable <textarea>"
    assert "ie-code-input" not in index_content, "Landing page must omit #ie-code-input"
    assert "ie-run-btn" not in index_content, "Landing page must omit mock #ie-run-btn"
    assert "#ie-code-input" not in showcase_js
    assert "#ie-run-btn" not in showcase_js

    assert 'id="ie-code-viewer"' in panel_integration
    assert 'id="ie-code-display"' in panel_integration
    assert 'id="ie-snippet-copy-btn"' in panel_integration
    assert 'href="tutorials/quick_start.html"' in panel_integration

    assert "Math.random" not in showcase_js, "showcase.js must not fabricate Math.random() latency"
    assert "Simulation Preview (Deterministic Fixture)" in index_content
    assert "Simulation Preview (Deterministic Fixture)" in showcase_js
    assert "Simulation Scenario Explorer" in readme_content
    assert "execute simulated SDK queries" not in readme_content


def test_showcase_canonical_terminology_and_validation_gate() -> None:
    """Verify docs/index.rst and docs/_static/showcase.js use canonical CONTEXT.md vocabulary."""
    repo_root = get_repo_root()
    docs_dir = repo_root / "docs"

    assert not validate_canonical_showcase_terminology(docs_dir / "index.rst")
    assert not validate_canonical_showcase_terminology(docs_dir / "_static" / "showcase.js")
    assert not validate_landing_showcase(docs_dir, repo_root)


def test_readonly_explorer_immediate_scenario_and_mode_updates_via_node() -> None:
    """Verify scenario selection, SDK/REST toggle, and snippet copy work immediately without a Run button."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node runtime not available for DOM mock evaluation")

    repo_root = get_repo_root()
    script_path = repo_root / "docs" / "_static" / "showcase.js"

    js_test = f"""
    const fs = require('fs');
    const code = fs.readFileSync('{script_path.as_posix()}', 'utf8');

    let copiedClipboard = '';
    Object.defineProperty(globalThis, 'navigator', {{
        configurable: true,
        value: {{
            clipboard: {{
                writeText: (txt) => {{
                    copiedClipboard = txt;
                    return Promise.resolve();
                }}
            }}
        }}
    }});

    function makeElement(id, tagName = 'div') {{
        const attrs = {{}};
        const classes = new Set();
        const handlers = {{}};
        return {{
            id,
            tagName,
            value: id === 'ie-dataset-select' ? 'records' : (id === 'cdm-form-select' ? 'VS' : 'part11'),
            textContent: '',
            innerHTML: '',
            className: '',
            clientWidth: 400,
            scrollWidth: 400,
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
            addEventListener: (ev, cb) => {{
                handlers[ev] = handlers[ev] || [];
                handlers[ev].push(cb);
            }},
            trigger: (ev) => {{
                (handlers[ev] || []).forEach(cb => cb({{ preventDefault: () => {{}} }}));
            }}
        }};
    }}

    const elements = {{
        '#ie-dataset-select': makeElement('ie-dataset-select', 'select'),
        '#ie-mode-sdk': makeElement('ie-mode-sdk', 'button'),
        '#ie-mode-rest': makeElement('ie-mode-rest', 'button'),
        '#ie-code-display': makeElement('ie-code-display', 'code'),
        '#ie-snippet-copy-btn': makeElement('ie-snippet-copy-btn', 'button'),
        '#ie-metric-badge': makeElement('ie-metric-badge', 'div'),
        '#ie-tab-table': makeElement('ie-tab-table', 'button'),
        '#ie-tab-json': makeElement('ie-tab-json', 'button'),
        '#ie-view-table': makeElement('ie-view-table', 'div'),
        '#ie-view-json': makeElement('ie-view-json', 'div'),
        '#ie-json-pre code': makeElement('ie-json-code', 'code'),
        '#ie-copy-btn': makeElement('ie-copy-btn', 'button'),
        '#cdm-form-select': makeElement('cdm-form-select', 'select'),
        '#cdm-validate-btn': makeElement('cdm-validate-btn', 'button'),
        '#cdm-metric-badge': makeElement('cdm-metric-badge', 'div'),
        '#cdm-tab-crf': makeElement('cdm-tab-crf', 'button'),
        '#cdm-tab-json': makeElement('cdm-tab-json', 'button'),
        '#cdm-view-crf': makeElement('cdm-view-crf', 'div'),
        '#cdm-view-json': makeElement('cdm-view-json', 'div'),
        '#cdm-json-pre code': makeElement('cdm-json-code', 'code'),
        '#cdm-copy-btn': makeElement('cdm-copy-btn', 'button'),
        '#aud-scenario-select': makeElement('aud-scenario-select', 'select'),
        '#aud-verify-btn': makeElement('aud-verify-btn', 'button'),
        '#aud-metric-badge': makeElement('aud-metric-badge', 'div'),
        '#aud-tab-table': makeElement('aud-tab-table', 'button'),
        '#aud-tab-json': makeElement('aud-tab-json', 'button'),
        '#aud-view-table': makeElement('aud-view-table', 'div'),
        '#aud-view-json': makeElement('aud-view-json', 'div'),
        '#aud-json-pre code': makeElement('aud-json-code', 'code'),
        '#aud-copy-btn': makeElement('aud-copy-btn', 'button')
    }};

    const panels = [
        makeElement('panel-integration', 'div'),
        makeElement('panel-datamanager', 'div'),
        makeElement('panel-auditor', 'div')
    ];
    const root = makeElement('interactive-showcase-root', 'div');
    root.querySelector = (sel) => elements[sel] || null;
    root.querySelectorAll = (sel) => sel === '.showcase-panel' ? panels : [];

    global.window = {{
        addEventListener: () => {{}},
        history: {{ replaceState: () => {{}} }},
        location: {{ hash: '' }}
    }};
    global.document = {{
        readyState: 'complete',
        getElementById: (id) => id === 'interactive-showcase-root' ? root : null,
        querySelectorAll: () => [],
        addEventListener: () => {{}},
        body: makeElement('body')
    }};

    eval(code);

    const out = {{}};
    out.initialSnippet = elements['#ie-code-display'].textContent;
    out.initialBadge = elements['#ie-metric-badge'].textContent;

    // Switch scenario to queries -> updates snippet, table, JSON, and badge immediately
    elements['#ie-dataset-select'].value = 'queries';
    elements['#ie-dataset-select'].trigger('change');
    out.queriesSnippet = elements['#ie-code-display'].textContent;
    out.queriesBadge = elements['#ie-metric-badge'].textContent;
    out.queriesJson = JSON.parse(elements['#ie-json-pre code'].textContent);

    // Switch mode to REST -> updates snippet to STUDY REST endpoint
    elements['#ie-mode-rest'].trigger('click');
    out.queriesRestSnippet = elements['#ie-code-display'].textContent;
    out.queriesRestLang = elements['#ie-code-display'].className;

    // Copy button copies active snippet
    elements['#ie-snippet-copy-btn'].trigger('click');
    out.copiedText = copiedClipboard;

    console.log(JSON.stringify(out));
    """

    res = subprocess.run(
        [node_bin, "-e", js_test],
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(res.stdout)

    assert 'ImednetSDK(simulation_mode=True)' in out["initialSnippet"]
    assert 'study_key="STUDY"' in out["initialSnippet"]
    assert "Simulation Preview (Deterministic Fixture)" in out["initialBadge"]
    assert re.search(r"\b\d+(?:\.\d+)?\s*ms\b", out["initialBadge"]) is None

    assert "QueryStatus.OPEN" in out["queriesSnippet"]
    assert "Study Queries Ledger (STUDY)" in out["queriesBadge"]
    assert all(item["studyKey"] == "STUDY" for item in out["queriesJson"])
    assert all("variable" in item for item in out["queriesJson"])

    assert out["queriesRestSnippet"].startswith("GET /api/v1/edc/studies/STUDY/queries HTTP/1.1")
    assert out["queriesRestLang"] == "language-http"
    assert out["copiedText"] == out["queriesRestSnippet"]


def _extract_showcase_mock_data() -> dict[str, object]:
    """Parse ``const MOCK_DATA = {...};`` from ``docs/_static/showcase.js`` without requiring Node.js."""
    repo_root = get_repo_root()
    js_source = (repo_root / "docs" / "_static" / "showcase.js").read_text(encoding="utf-8")
    start_marker = "const MOCK_DATA = "
    start_idx = js_source.index(start_marker) + len(start_marker)
    end_idx = js_source.index("const SNIPPETS =", start_idx)
    raw_block = js_source[start_idx:end_idx].strip()
    if raw_block.endswith(";"):
        raw_block = raw_block[:-1].rstrip()

    # Quote unquoted JS object keys outside string literals while ignoring // line comments
    out_chars: list[str] = []
    i = 0
    n = len(raw_block)
    in_string = False
    escape = False

    while i < n:
        ch = raw_block[i]
        if in_string:
            out_chars.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out_chars.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n and raw_block[i + 1] == "/":
            newline_pos = raw_block.find("\n", i)
            i = n if newline_pos == -1 else newline_pos
            continue

        key_match = re.match(r"([A-Za-z_]\w*)(\s*:)", raw_block[i:])
        if key_match:
            ident = key_match.group(1)
            if ident not in {"true", "false", "null"}:
                out_chars.append(f'"{ident}"{key_match.group(2)}')
                i += len(key_match.group(0))
                continue

        out_chars.append(ch)
        i += 1

    cleaned = "".join(out_chars).strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    parsed = json.loads(cleaned)
    assert isinstance(parsed, dict)
    return parsed


def test_showcase_mock_forms_match_canonical_datadict_form_contract() -> None:
    """Verify MOCK_DATA.forms matches imednet.datadict.models.Form with zero fabricated keys."""
    from imednet.datadict import (
        BusinessLogicRule,
        Choice,
        LogicTreeEvaluator,
        Variable,
    )
    from imednet.datadict.models import Form as DatadictForm

    mock_data = _extract_showcase_mock_data()
    forms = mock_data.get("forms")
    records_by_domain = mock_data.get("records")
    assert isinstance(forms, dict)
    assert isinstance(records_by_domain, dict)
    assert set(forms.keys()) == {"VS", "DM", "AE", "LB"}

    allowed_form_keys = set(DatadictForm.model_fields.keys())
    allowed_var_keys = set(Variable.model_fields.keys())
    allowed_choice_keys = set(Choice.model_fields.keys())
    allowed_rule_keys = set(BusinessLogicRule.model_fields.keys())
    evaluator = LogicTreeEvaluator()

    for domain_key, raw_form in forms.items():
        assert isinstance(raw_form, dict)
        fabricated_form_keys = set(raw_form.keys()) - allowed_form_keys
        assert not fabricated_form_keys, (
            f"Form '{domain_key}' contains fabricated non-canonical keys: {sorted(fabricated_form_keys)}"
        )

        parsed_form = DatadictForm.model_validate(raw_form)
        assert not parsed_form.model_extra
        assert not parsed_form.extraAttributes
        assert len(parsed_form.variables) > 0
        assert len(parsed_form.businessLogic) > 0

        raw_vars = raw_form.get("variables", [])
        assert isinstance(raw_vars, list)
        for raw_var, parsed_var in zip(raw_vars, parsed_form.variables, strict=True):
            assert isinstance(raw_var, dict)
            fabricated_var_keys = set(raw_var.keys()) - allowed_var_keys
            assert not fabricated_var_keys, (
                f"Variable in '{domain_key}' contains fabricated keys: {sorted(fabricated_var_keys)}"
            )
            assert not parsed_var.model_extra
            assert not parsed_var.extraAttributes

            raw_choices = raw_var.get("choices", [])
            assert isinstance(raw_choices, list)
            for raw_choice, parsed_choice in zip(raw_choices, parsed_var.choices, strict=True):
                assert isinstance(raw_choice, dict)
                fabricated_choice_keys = set(raw_choice.keys()) - allowed_choice_keys
                assert not fabricated_choice_keys, (
                    f"Choice in '{domain_key}.{parsed_var.variableName}' contains fabricated keys: "
                    f"{sorted(fabricated_choice_keys)}"
                )
                assert not parsed_choice.model_extra
                assert not parsed_choice.extraAttributes

        raw_rules = raw_form.get("businessLogic", [])
        assert isinstance(raw_rules, list)
        for raw_rule, parsed_rule in zip(raw_rules, parsed_form.businessLogic, strict=True):
            assert isinstance(raw_rule, dict)
            fabricated_rule_keys = set(raw_rule.keys()) - allowed_rule_keys
            assert not fabricated_rule_keys, (
                f"BusinessLogicRule in '{domain_key}' contains fabricated keys: "
                f"{sorted(fabricated_rule_keys)}"
            )
            assert not parsed_rule.model_extra
            assert not parsed_rule.extraAttributes

        domain_records = records_by_domain.get(domain_key)
        assert isinstance(domain_records, list) and domain_records
        sample_record = domain_records[0]
        assert isinstance(sample_record, dict)
        eval_report = evaluator.evaluate_rules(
            parsed_form.businessLogic,
            sample_record,
            form_key=parsed_form.formKey,
        )
        assert len(eval_report.results) == len(parsed_form.businessLogic)
        assert all(not r.skipped for r in eval_report.results)


def test_showcase_mock_audits_match_record_revision_and_validate_chain() -> None:
    """Verify MOCK_DATA.audits matches all 19 RecordRevision attributes and passes validate_chain."""
    from imednet.models.record_revisions import RecordRevision

    mock_data = _extract_showcase_mock_data()
    audits = mock_data.get("audits")
    assert isinstance(audits, list) and len(audits) >= 2

    expected_aliases = {
        field_info.alias or field_name
        for field_name, field_info in RecordRevision.model_fields.items()
    }
    assert len(expected_aliases) == 19

    for idx, raw_audit in enumerate(audits):
        assert isinstance(raw_audit, dict)
        actual_keys = set(raw_audit.keys())
        missing_keys = expected_aliases - actual_keys
        fabricated_keys = actual_keys - expected_aliases
        assert not missing_keys, (
            f"Audit revision [{idx}] missing canonical RecordRevision keys: {sorted(missing_keys)}"
        )
        assert not fabricated_keys, (
            f"Audit revision [{idx}] contains fabricated keys: {sorted(fabricated_keys)}"
        )
        rev = RecordRevision.model_validate(raw_audit)
        assert not getattr(rev, "model_extra", None)

    validated_chain = RecordRevision.validate_chain(audits)
    assert len(validated_chain) == len(audits)


def test_persona_routing_and_datadict_subcommands_surfaced() -> None:
    """Verify persona cards, panel callouts, and README route to canonical guides and surface all 8 datadict subcommands."""
    from imednet.cli import get_parser

    repo_root = get_repo_root()
    index_rst = (repo_root / "docs" / "index.rst").read_text(encoding="utf-8")
    readme_md = (repo_root / "README.md").read_text(encoding="utf-8")
    datadict_rst = (repo_root / "docs" / "how-to" / "data_dictionary.rst").read_text(
        encoding="utf-8"
    )
    audit_rst = (repo_root / "docs" / "how-to" / "audit_compliance.rst").read_text(encoding="utf-8")

    parser = get_parser()
    datadict_subcommands = (
        "from-csv",
        "from-odm",
        "to-odm",
        "from-redcap",
        "from-api",
        "validate",
        "diff",
        "schema",
    )
    import argparse

    # Verify CLI parser still registers these exact 8 subcommands
    subparsers_actions = [
        act for act in parser._actions if isinstance(act, argparse._SubParsersAction)
    ]
    datadict_parser = subparsers_actions[0].choices["datadict"]
    datadict_sub_actions = [
        act for act in datadict_parser._actions if isinstance(act, argparse._SubParsersAction)
    ]
    registered_datadict_cmds = set(datadict_sub_actions[0].choices.keys())
    assert registered_datadict_cmds == set(datadict_subcommands)

    for cmd in datadict_subcommands:
        assert cmd in datadict_rst, (
            f"Subcommand '{cmd}' missing from docs/how-to/data_dictionary.rst"
        )
        assert cmd in index_rst, f"Subcommand '{cmd}' missing from docs/index.rst"
        assert cmd in readme_md, f"Subcommand '{cmd}' missing from README.md"

    assert "docs/how-to/data_dictionary.rst" in readme_md
    assert "how-to/data_dictionary" in index_rst
    assert "how-to/audit_compliance" in index_rst
    assert "LogicTreeEvaluator" in datadict_rst
    assert "RecordRevisionsEndpoint" in audit_rst
    assert "extract_audit_trail" in audit_rst
    assert "validate_chain" in audit_rst

    # Isolate role cards in docs/index.rst
    cards_section = index_rst[
        index_rst.index(".. rubric:: Clinical Data Managers") : index_rst.index(
            '<div id="interactive-showcase-root"'
        )
    ]
    cdm_card = cards_section[: cards_section.index(".. rubric:: Integration Engineers")]
    aud_card = cards_section[cards_section.index(".. rubric:: Regulatory Compliance Auditors") :]

    for expected_cdm_doc in (
        "how-to/data_dictionary",
        "how-to/workflows/uat_workflow",
        "how-to/register_subjects",
        "how-to/workflows/bulk_submission",
    ):
        assert expected_cdm_doc in cdm_card
    for forbidden_cdm_doc in ("how-to/verification", "how-to/form_builder"):
        assert forbidden_cdm_doc not in cdm_card

    for expected_aud_doc in (
        "how-to/audit_compliance",
        "how-to/duckdb_integration",
        "how-to/export_destinations",
        "reference/governance",
        "reference/VPAT",
    ):
        assert expected_aud_doc in aud_card
    for forbidden_aud_doc in (
        "reference/project_standards",
        "reference/security_exceptions",
        "adr/index",
    ):
        assert forbidden_aud_doc not in aud_card

    # Isolate #panel-datamanager and #panel-auditor callouts
    panel_cdm = index_rst[
        index_rst.index('id="panel-datamanager"') : index_rst.index('id="panel-auditor"')
    ]
    panel_aud = index_rst[
        index_rst.index('id="panel-auditor"') : index_rst.index("Documentation Overview")
    ]

    for href in (
        "how-to/data_dictionary.html",
        "how-to/workflows/uat_workflow.html",
        "how-to/register_subjects.html",
        "how-to/workflows/bulk_submission.html",
    ):
        assert href in panel_cdm
    assert "how-to/form_builder.html" not in panel_cdm

    for href in (
        "how-to/audit_compliance.html",
        "how-to/duckdb_integration.html",
        "how-to/export_destinations.html",
        "reference/governance.html",
        "reference/VPAT.html",
    ):
        assert href in panel_aud


def test_form_builder_preset_snippet_executes_and_example_documents_cli_options() -> None:
    """Verify form_builder.rst preset snippet runs without NameError and build_form_payload.py lists all CLI flags."""
    import textwrap

    repo_root = get_repo_root()
    form_builder_rst = (repo_root / "docs" / "how-to" / "form_builder.rst").read_text(
        encoding="utf-8"
    )
    assert ":doc:`data_dictionary`" in form_builder_rst
    assert "formdez_save.php" in form_builder_rst
    assert "PHPSESSID" in form_builder_rst
    assert "CSRFKey" in form_builder_rst

    match = re.search(
        r"\.\.\s+(?:testcode|code-block::\s*python)::\n\n((?:[ ]{4,}.*\n|\n)+)",
        form_builder_rst,
    )
    assert match, "Python preset snippet not found in docs/how-to/form_builder.rst"
    snippet_code = textwrap.dedent(match.group(1))

    namespace: dict[str, object] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(snippet_code, "form_builder.rst", "exec"), namespace)  # noqa: S102

    assert "FormBuilder" in namespace
    assert callable(namespace.get("build_my_form"))
    layout = namespace.get("layout")
    assert hasattr(layout, "pages") and len(layout.pages) == 1  # type: ignore[union-attr]

    example_py = (repo_root / "examples" / "build_form_payload.py").read_text(encoding="utf-8")
    example_tree = ast.parse(example_py)
    docstring = ast.get_docstring(example_tree) or ""
    for required_flag in (
        "--preset",
        "--base-url",
        "--phpsessid",
        "--csrf",
        "--form-id",
        "--comm-id",
        "--revision",
    ):
        assert required_flag in docstring, (
            f"Required CLI option '{required_flag}' missing from examples/build_form_payload.py docstring"
        )
    assert "IMEDNET_API_KEY" not in docstring
    assert "IMEDNET_SECURITY_KEY" not in docstring
