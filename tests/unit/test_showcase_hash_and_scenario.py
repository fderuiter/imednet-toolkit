"""Automated regression tests for showcase URL hash routing and scenario selection."""

from __future__ import annotations

import re
from pathlib import Path


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
