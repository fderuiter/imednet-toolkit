"""Integration tests for scripts/typecheck_docs.py mypy subprocess verification."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.typecheck_docs import main, typecheck_documentation


def test_repo_documentation_snippets_pass_typecheck() -> None:
    """Verify all RST, literalinclude, showcase.js, Markdown, and docstring snippets pass mypy."""
    repo_root = Path(__file__).resolve().parents[2]
    count, errors = typecheck_documentation(repo_root)
    assert count >= 50
    assert not errors, f"Expected repository documentation snippets to type-check, got: {errors}"
    assert not (repo_root / "docs" / "_build" / "snippets").exists()


def test_typecheck_docs_catches_rst_markdown_and_docstring_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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

    output = capsys.readouterr().out
    assert "Extracted and checked 3 documentation Python snippet unit(s)." in output
    assert "guide.rst" in output and "NonExistentSdkSymbol" in output
    assert "README.md" in output and "nonexistent_endpoint_attr" in output
    assert "example.py" in output and "NonExistentExceptionClass" in output


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


def test_typecheck_docs_catches_literalinclude_showcase_js_and_untyped_defs(
    tmp_path: Path,
) -> None:
    """Verify literalinclude slices, showcase.js SNIPPETS.sdk templates, and untyped function bodies are checked."""
    docs_dir = tmp_path / "docs"
    static_dir = docs_dir / "_static"
    examples_dir = tmp_path / "examples"
    static_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    (examples_dir / "sliced_tutorial.py").write_text(
        "# [start]\n"
        "from imednet import ImednetSDK\n\n"
        "def untyped_helper():\n"
        "    sdk = ImednetSDK()\n"
        "    sdk.studies.nonexistent_method()\n"
        "# [end]\n",
        encoding="utf-8",
    )
    (docs_dir / "tutorial.rst").write_text(
        "Tutorial\n"
        "========\n\n"
        ".. literalinclude:: /../examples/sliced_tutorial.py\n"
        "   :language: python\n"
        "   :start-after: # [start]\n"
        "   :end-before: # [end]\n",
        encoding="utf-8",
    )
    (static_dir / "showcase.js").write_text(
        "const SNIPPETS = {\n"
        "    sdk: {\n"
        '        studies: `from imednet import ImednetSDK\\nsdk = ImednetSDK()\\nlen(sdk.studies.list())`\n'
        "    },\n"
        "    rest: {}\n"
        "};\n",
        encoding="utf-8",
    )

    count, errors = typecheck_documentation(tmp_path)
    assert count == 2
    assert any("sliced_tutorial.py:" in err and "nonexistent_method" in err for err in errors)
    assert any("showcase.js:" in err and "Sized" in err for err in errors)
