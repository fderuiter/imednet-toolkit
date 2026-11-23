"""Unit tests for scripts/validate_diagrams.py."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_diagrams import (
    get_code_symbols,
    validate_diagrams,
    validate_registry_imports,
)


def test_repository_diagrams_pass_validation() -> None:
    """All shipped .mmd diagrams, registry.json imports, and inline RST diagrams validate cleanly."""
    repo_root = Path(__file__).resolve().parents[2]
    errors, total_rendered = validate_diagrams(str(repo_root))
    assert errors == []
    assert total_rendered >= 15


def test_validate_diagrams_checks_inline_mermaid_symbols_and_registry_imports(
    tmp_path: Path,
) -> None:
    """validate_diagrams flags unknown inline Mermaid participants and invalid registry.json imports."""
    diagrams_dir = tmp_path / "docs" / "diagrams"
    workflows_dir = tmp_path / "docs" / "how-to"
    diagrams_dir.mkdir(parents=True)
    workflows_dir.mkdir(parents=True)

    registry_path = diagrams_dir / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "User": "End User / Developer",
                "GenericEndpoint": "imednet.core.endpoint.base.GenericEndpoint",
                "CentralizedMapper": "imednet.spi.sinks.CentralizedMapper",
            }
        ),
        encoding="utf-8",
    )

    rst_path = workflows_dir / "flow.rst"
    rst_path.write_text(
        "Flow\n====\n\n"
        ".. mermaid::\n\n"
        "    sequenceDiagram\n"
        "        accTitle: Valid Inline Sequence\n"
        "        participant User\n"
        "        participant Facade as ImednetFacade.poll_jobs\n"
        "        participant Poller as JobPoller\n"
        "        User->>Facade: poll_jobs()\n"
        "        Facade->>Poller: poll_many()\n",
        encoding="utf-8",
    )

    errors, rendered = validate_diagrams(str(tmp_path), min_rendered=1)
    assert errors == []
    assert rendered == 1

    # Unknown symbol in inline .. mermaid:: block must fail
    rst_path.write_text(
        "Flow\n====\n\n"
        ".. mermaid::\n\n"
        "    sequenceDiagram\n"
        "        accTitle: Stale Inline Sequence\n"
        "        participant User\n"
        "        participant Poller as PollingManager\n"
        "        User->>Poller: poll()\n",
        encoding="utf-8",
    )
    errors_inline, _ = validate_diagrams(str(tmp_path), min_rendered=1)
    assert any("PollingManager" in err for err in errors_inline)

    # Restore valid inline diagram and test broken registry.json Python import
    rst_path.write_text(
        "Flow\n====\n\n"
        ".. mermaid::\n\n"
        "    sequenceDiagram\n"
        "        accTitle: Valid Inline Sequence\n"
        "        participant User\n"
        "        participant Poller as JobPoller\n"
        "        User->>Poller: poll_many()\n",
        encoding="utf-8",
    )
    bad_registry = {
        "User": "End User / Developer",
        "BrokenClass": "imednet.core.endpoint.base.BaseEndpoint",
        "BrokenMod": "imednet.validation.data_dictionary",
    }
    import_errors = validate_registry_imports(bad_registry, str(tmp_path))
    assert any("BaseEndpoint" in err for err in import_errors)
    assert any("imednet.validation.data_dictionary" in err for err in import_errors)


def test_validate_diagrams_scans_plugins_tui_symbols() -> None:
    """get_code_symbols discovers classes and async methods from packages/plugins-tui."""
    repo_root = Path(__file__).resolve().parents[2]
    tui_src = repo_root / "packages" / "plugins-tui" / "src" / "imednet_tui"
    symbols = get_code_symbols([str(tui_src)])
    assert "ImednetTuiApp" in symbols
