"""Unit tests for strict documentation and showcase snippet extraction and type-checking."""

from __future__ import annotations

from pathlib import Path

from scripts.typecheck_docs import (
    _combine_file_snippets,
    extract_all_snippets,
    extract_markdown_python_blocks,
    extract_rst_python_blocks,
    extract_showcase_js_sdk_templates,
)


def test_extract_rst_python_blocks_captures_testcode_and_code_block() -> None:
    """Verify both .. testcode:: and .. code-block:: python/py directives are extracted."""
    content = (
        "Guide\n"
        "=====\n"
        "\n"
        ".. testcode::\n"
        "\n"
        "   x: int = 1\n"
        "\n"
        ".. code-block:: python\n"
        "   :linenos:\n"
        "\n"
        "   y: str = 'ok'\n"
        "\n"
        ".. code-block:: bash\n"
        "\n"
        "   echo ignored\n"
    )

    blocks = extract_rst_python_blocks(content)
    assert len(blocks) == 2
    assert "x: int = 1" in blocks[0]
    assert "y: str = 'ok'" in blocks[1]


def test_extract_markdown_and_showcase_js_snippets() -> None:
    """Verify Markdown fenced Python blocks and showcase.js SDK templates are extracted."""
    md_blocks = extract_markdown_python_blocks(
        "# Title\n\n```python\nfrom imednet import ImednetSDK\n```\n\n```json\n{}\n```\n"
    )
    assert len(md_blocks) == 1
    assert "from imednet import ImednetSDK" in md_blocks[0]

    js_templates = extract_showcase_js_sdk_templates(
        "const SNIPPETS = {\n"
        "  sdk: {\n"
        "    records: `from imednet import ImednetSDK\\nsdk = ImednetSDK()`,\n"
        "    queries: `from imednet import ImednetSDK\\nq = list(ImednetSDK().queries.list('S'))`,\n"
        "  },\n"
        "  rest: {\n"
        "    records: `GET /api/v1/edc/studies HTTP/1.1`,\n"
        "  },\n"
        "};\n"
    )
    assert set(js_templates.keys()) == {"records", "queries"}
    assert "sdk = ImednetSDK()" in js_templates["records"]
    assert "list(ImednetSDK().queries.list('S'))" in js_templates["queries"]


def test_combine_file_snippets_hoists_future_annotations() -> None:
    """Verify from __future__ import annotations in later blocks is hoisted to module top."""
    module_src = _combine_file_snippets(
        [
            "a = 1\n",
            "from __future__ import annotations\nb: int = 2\n",
        ]
    )
    compile(module_src, "<snippets>", "exec")
    non_comment_lines = [
        line for line in module_src.splitlines() if line.strip() and not line.startswith("#")
    ]
    assert non_comment_lines[0] == "from __future__ import annotations"


def test_extract_all_snippets_mirrors_paths_without_stem_collisions(tmp_path: Path) -> None:
    """Verify files sharing stems (quick_start.rst, index.rst, README.md) never overwrite each other."""
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "tutorials").mkdir(parents=True)
    (repo_root / "docs" / "how-to").mkdir(parents=True)
    (repo_root / "docs" / "_static").mkdir(parents=True)
    (repo_root / "docs" / "superpowers").mkdir(parents=True)
    (repo_root / "docs" / "agents").mkdir(parents=True)
    (repo_root / "packages" / "core").mkdir(parents=True)

    (repo_root / "docs" / "tutorials" / "quick_start.rst").write_text(
        ".. testcode::\n\n   tutorial_val = 1\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "how-to" / "quick_start.rst").write_text(
        ".. code-block:: python\n\n   howto_val = 2\n",
        encoding="utf-8",
    )
    (repo_root / "README.md").write_text(
        "```python\nroot_readme = 3\n```\n",
        encoding="utf-8",
    )
    (repo_root / "packages" / "core" / "README.md").write_text(
        "```python\npkg_readme = 4\n```\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "_static" / "showcase.js").write_text(
        "const SNIPPETS = {\n  sdk: {\n    studies: `js_val = 5`,\n  },\n  rest: {},\n};\n",
        encoding="utf-8",
    )
    # Archived directories must be ignored
    (repo_root / "docs" / "superpowers" / "archived.md").write_text(
        "```python\narchived_val = 99\n```\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "agents" / "notes.rst").write_text(
        ".. code-block:: python\n\n   agent_val = 100\n",
        encoding="utf-8",
    )

    snippets_dir = repo_root / "docs" / "_build" / "snippets"
    total_snippets, written = extract_all_snippets(repo_root, snippets_dir)

    assert total_snippets == 5
    assert len(written) == 5
    rel_paths = {p.relative_to(snippets_dir).as_posix() for p in written}
    assert rel_paths == {
        "docs/tutorials/quick_start_rst.py",
        "docs/how_to/quick_start_rst.py",
        "README_md.py",
        "packages/core/README_md.py",
        "docs/_static/showcase_js/studies.py",
    }
