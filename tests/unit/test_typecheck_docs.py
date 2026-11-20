"""Unit tests for scripts/typecheck_docs.py snippet extraction and mypy verification."""

from __future__ import annotations

from pathlib import Path

from scripts.typecheck_docs import main, typecheck_documentation


def test_repo_documentation_snippets_pass_typecheck() -> None:
    """Verify all RST, Markdown, and docstring Python snippets in the repository pass mypy."""
    repo_root = Path(__file__).resolve().parents[2]
    count, errors = typecheck_documentation(repo_root)
    assert count >= 20
    assert not errors, f"Expected repository documentation snippets to type-check, got: {errors}"
    assert not (repo_root / "docs" / "_build" / "snippets").exists()


def test_typecheck_docs_catches_rst_markdown_and_docstring_errors(tmp_path: Path) -> None:
    """Verify bad imports or symbol mismatches in RST, README, and docstrings fail with code 1."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "guide.rst").write_text(
        "Guide\n=====\n\n.. code-block:: python\n\n   from imednet import NonExistentSdkSymbol\n",
        encoding="utf-8",
    )

    (tmp_path / "README.md").write_text(
        "# Demo\n\n"
        "```python\n"
        "from imednet import ImednetSDK\n\n"
        "sdk = ImednetSDK()\n"
        "sdk.nonexistent_endpoint_attr()\n"
        "```\n",
        encoding="utf-8",
    )

    pkg_src = tmp_path / "packages" / "demo" / "src" / "demo_pkg"
    pkg_src.mkdir(parents=True)
    (pkg_src / "example.py").write_text(
        '"""Demo module.\n\n'
        ".. code-block:: python\n\n"
        "    from imednet.errors import NonExistentExceptionClass\n"
        '"""\n',
        encoding="utf-8",
    )

    exit_code = main(["--root-dir", str(tmp_path)])
    assert exit_code == 1

    count, errors = typecheck_documentation(tmp_path)
    assert count == 3
    assert any("guide.rst" in err and "NonExistentSdkSymbol" in err for err in errors)
    assert any("README.md" in err and "nonexistent_endpoint_attr" in err for err in errors)
    assert any("example.py" in err and "NonExistentExceptionClass" in err for err in errors)


def test_typecheck_docs_follows_imports_silently_for_package_internals(tmp_path: Path) -> None:
    """Verify valid snippets importing package classes with getattr internals pass cleanly."""
    pkg_src = tmp_path / "packages" / "demo" / "src" / "demo_pkg"
    pkg_src.mkdir(parents=True)
    (pkg_src / "__init__.py").write_text("", encoding="utf-8")
    (pkg_src / "operator.py").write_text(
        '"""Demo operator."""\n\n'
        "from __future__ import annotations\n\n"
        "class DemoOperator:\n"
        '    """Demo class."""\n\n'
        "    @property\n"
        "    def output_path(self) -> str | None:\n"
        '        """Return output path."""\n'
        '        return getattr(self, "_output_path", None)\n',
        encoding="utf-8",
    )

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "usage.rst").write_text(
        "Usage\n=====\n\n"
        ".. code-block:: python\n\n"
        "   from demo_pkg.operator import DemoOperator\n\n"
        "   op = DemoOperator()\n"
        "   path: str | None = op.output_path\n",
        encoding="utf-8",
    )

    count, errors = typecheck_documentation(tmp_path)
    assert count == 1
    assert not errors, f"Expected zero errors with --follow-imports=silent, got: {errors}"
