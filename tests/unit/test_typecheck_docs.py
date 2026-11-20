"""Unit tests for scripts/typecheck_docs.py snippet extraction and syntax diagnostics."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.typecheck_docs as typecheck_docs_mod
from scripts.typecheck_docs import (
    collect_snippets,
    extract_docstring_snippets,
    extract_markdown_python_blocks,
    extract_rst_python_blocks,
    main,
    typecheck_documentation,
)


def test_collect_snippets_extracts_rst_markdown_and_docstrings(tmp_path: Path) -> None:
    """Verify collect_snippets gathers RST testcode/code-block, Markdown, and docstring blocks."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "guide.rst").write_text(
        "Guide\n=====\n\n.. testcode::\n\n   x = 1\n\n.. code-block:: python\n\n   y = 2\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# Demo\n\n```python\nz = 3\n```\n",
        encoding="utf-8",
    )
    pkg_src = tmp_path / "packages" / "demo" / "src" / "demo_pkg"
    pkg_src.mkdir(parents=True)
    py_file = pkg_src / "example.py"
    py_file.write_text(
        '"""Demo module.\n\n.. code-block:: python\n\n    w = 4\n"""\n',
        encoding="utf-8",
    )

    tc_blocks, cb_blocks = extract_rst_python_blocks(
        (docs_dir / "guide.rst").read_text(encoding="utf-8"), docs_dir / "guide.rst"
    )
    assert len(tc_blocks) == 1
    assert len(cb_blocks) == 1
    assert len(extract_markdown_python_blocks((tmp_path / "README.md").read_text(), py_file)) == 1
    assert len(extract_docstring_snippets(py_file, 0)) == 1

    snippets = collect_snippets(tmp_path)
    assert len(snippets) == 4


def test_typecheck_docs_reports_syntax_errors_and_maps_mypy_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify syntax errors and mocked mypy diagnostics map back to source files and line numbers."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    guide_rst = docs_dir / "guide.rst"
    guide_rst.write_text(
        "Guide\n=====\n\n"
        ".. code-block:: python\n\n"
        "   def broken(\n\n"
        ".. code-block:: python\n\n"
        "   value: int = 'bad'\n",
        encoding="utf-8",
    )

    def _fake_run(cmd, **_kwargs):
        snippet_dir = Path(cmd[-1])
        snippet_files = sorted(snippet_dir.glob("*.py"))
        assert len(snippet_files) == 1
        fname = snippet_files[0].name
        stdout = f'{snippet_dir / fname}:1: error: Incompatible types in assignment [assignment]\n'
        return SimpleNamespace(returncode=1, stdout=stdout, stderr="")

    monkeypatch.setattr(typecheck_docs_mod.subprocess, "run", _fake_run)

    exit_code = main(["--root-dir", str(tmp_path)])
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "SyntaxError" in output
    assert "guide.rst" in output
    assert "Incompatible types in assignment" in output

    empty_count, empty_errors = typecheck_documentation(tmp_path / "empty")
    assert empty_count == 0
    assert empty_errors == []
