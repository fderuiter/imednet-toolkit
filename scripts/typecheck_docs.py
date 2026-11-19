"""Extract and type-check Python code snippets across RST docs, Markdown READMEs, and docstrings."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_DOC_DIRS = {"_build", "agents", "superpowers"}
RST_DIRECTIVE_RE = re.compile(r"^(\s*)\.\.\s+(testcode|code-block|sourcecode)::(?:\s*(\S+))?\s*$")
RST_OPTION_RE = re.compile(r"^:[a-zA-Z0-9_-]+:")
MD_FENCE_OPEN_RE = re.compile(r"^(\s*)```(?:python|py)\s*$")
MD_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")
MYPY_DIAG_RE = re.compile(r"^.*?([^/\\:]+\.py):(\d+):(.*)$")


@dataclass(frozen=True)
class ExtractedSnippet:
    """A single extracted Python snippet unit and its line map back to source files."""

    filename: str
    code: str
    line_map: dict[int, tuple[Path, int]]


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DOC_DIRS for part in path.parts)


def extract_rst_python_blocks(
    text: str,
    source_path: Path,
    *,
    line_offset: int = 0,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Extract (testcode_blocks, code_blocks) as lists of (1-based source line, dedented code)."""
    lines = text.splitlines()
    testcode_blocks: list[tuple[int, str]] = []
    code_blocks: list[tuple[int, str]] = []
    i = 0
    n = len(lines)

    while i < n:
        match = RST_DIRECTIVE_RE.match(lines[i])
        if not match:
            i += 1
            continue

        base_indent = len(match.group(1))
        directive = match.group(2)
        language = (match.group(3) or "").lower()
        is_python = directive == "testcode" or (
            directive in ("code-block", "sourcecode") and language in ("python", "py")
        )
        i += 1
        if not is_python:
            continue

        # Skip directive options such as :caption:, :linenos:, :hide:, :emphasize-lines:
        while i < n:
            stripped = lines[i].strip()
            if stripped == "":
                i += 1
                break
            curr_indent = len(lines[i]) - len(lines[i].lstrip())
            if curr_indent > base_indent and RST_OPTION_RE.match(stripped):
                i += 1
            else:
                break

        while i < n and lines[i].strip() == "":
            i += 1
        if i >= n:
            break

        block_indent = len(lines[i]) - len(lines[i].lstrip())
        if block_indent <= base_indent:
            continue

        start_lineno = line_offset + i + 1
        buf: list[str] = []
        while i < n:
            curr = lines[i]
            if curr.strip() == "":
                buf.append("")
                i += 1
                continue
            curr_indent = len(curr) - len(curr.lstrip())
            if curr_indent >= block_indent:
                buf.append(curr[block_indent:])
                i += 1
            else:
                break

        while buf and buf[-1] == "":
            buf.pop()

        if buf:
            code = "\n".join(buf)
            if directive == "testcode":
                testcode_blocks.append((start_lineno, code))
            else:
                code_blocks.append((start_lineno, code))

    return testcode_blocks, code_blocks


def extract_markdown_python_blocks(
    text: str,
    source_path: Path,
    *,
    line_offset: int = 0,
) -> list[tuple[int, str]]:
    """Extract fenced ```python / ```py blocks as (1-based source line, dedented code)."""
    lines = text.splitlines()
    blocks: list[tuple[int, str]] = []
    i = 0
    n = len(lines)

    while i < n:
        match = MD_FENCE_OPEN_RE.match(lines[i])
        if not match:
            i += 1
            continue

        fence_indent = len(match.group(1))
        start_lineno = line_offset + i + 2
        i += 1
        buf: list[str] = []
        while i < n and not MD_FENCE_CLOSE_RE.match(lines[i]):
            raw = lines[i]
            buf.append(raw[fence_indent:] if len(raw) >= fence_indent else raw)
            i += 1

        code = textwrap.dedent("\n".join(buf)).rstrip()
        if code.strip():
            blocks.append((start_lineno, code))
        i += 1

    return blocks


def extract_docstring_snippets(py_path: Path, counter_start: int) -> list[ExtractedSnippet]:
    """Extract RST and Markdown Python code blocks inside module/class/function docstrings."""
    content = py_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(content, filename=str(py_path))
    except SyntaxError:
        return []

    snippets: list[ExtractedSnippet] = []
    idx = counter_start
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first_stmt = node.body[0]
        if not (
            isinstance(first_stmt, ast.Expr)
            and isinstance(first_stmt.value, ast.Constant)
            and isinstance(first_stmt.value.value, str)
        ):
            continue

        raw_doc = first_stmt.value.value
        doc_lineno = first_stmt.lineno
        # Preserve line alignment when the opening triple-quote is followed immediately by text vs newline
        raw_lines = raw_doc.splitlines()
        if len(raw_lines) > 1:
            dedented = raw_lines[0] + "\n" + textwrap.dedent("\n".join(raw_lines[1:]))
        else:
            dedented = raw_doc

        tc_blocks, cb_blocks = extract_rst_python_blocks(
            dedented, py_path, line_offset=doc_lineno - 1
        )
        md_blocks = extract_markdown_python_blocks(dedented, py_path, line_offset=doc_lineno - 1)

        for start_line, code in [*tc_blocks, *cb_blocks, *md_blocks]:
            idx += 1
            code_lines = code.splitlines()
            line_map = {
                offset + 1: (py_path, start_line + offset) for offset in range(len(code_lines))
            }
            snippets.append(
                ExtractedSnippet(
                    filename=f"doc_{idx}_{py_path.stem}.py",
                    code=code,
                    line_map=line_map,
                )
            )

    return snippets


def collect_snippets(repo_root: Path) -> list[ExtractedSnippet]:
    """Collect all Python code snippets from RST docs, Markdown files, and package docstrings."""
    snippets: list[ExtractedSnippet] = []
    idx = 0

    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        for rst_file in sorted(docs_dir.rglob("*.rst")):
            if _is_excluded(rst_file.relative_to(repo_root)):
                continue
            text = rst_file.read_text(encoding="utf-8")
            tc_blocks, cb_blocks = extract_rst_python_blocks(text, rst_file)
            if tc_blocks:
                idx += 1
                combined_lines: list[str] = []
                line_map: dict[int, tuple[Path, int]] = {}
                curr_line = 1
                for start_line, code in tc_blocks:
                    for offset, line in enumerate(code.splitlines()):
                        line_map[curr_line] = (rst_file, start_line + offset)
                        combined_lines.append(line)
                        curr_line += 1
                    combined_lines.append("")
                    curr_line += 1
                snippets.append(
                    ExtractedSnippet(
                        filename=f"rst_tc_{idx}_{rst_file.stem}.py",
                        code="\n".join(combined_lines).rstrip(),
                        line_map=line_map,
                    )
                )

            for start_line, code in cb_blocks:
                idx += 1
                code_lines = code.splitlines()
                line_map = {
                    offset + 1: (rst_file, start_line + offset) for offset in range(len(code_lines))
                }
                snippets.append(
                    ExtractedSnippet(
                        filename=f"rst_cb_{idx}_{rst_file.stem}.py",
                        code=code,
                        line_map=line_map,
                    )
                )

    markdown_candidates: list[Path] = []
    root_readme = repo_root / "README.md"
    if root_readme.is_file():
        markdown_candidates.append(root_readme)
    packages_dir = repo_root / "packages"
    if packages_dir.exists():
        markdown_candidates.extend(sorted(packages_dir.glob("*/README.md")))
    examples_dir = repo_root / "examples"
    if examples_dir.exists():
        markdown_candidates.extend(sorted(examples_dir.rglob("*.md")))
    if docs_dir.exists():
        markdown_candidates.extend(
            p for p in sorted(docs_dir.rglob("*.md")) if not _is_excluded(p.relative_to(repo_root))
        )

    seen_md: set[Path] = set()
    for md_file in markdown_candidates:
        resolved = md_file.resolve()
        if resolved in seen_md or not md_file.is_file():
            continue
        seen_md.add(resolved)
        text = md_file.read_text(encoding="utf-8")
        for start_line, code in extract_markdown_python_blocks(text, md_file):
            idx += 1
            code_lines = code.splitlines()
            line_map = {
                offset + 1: (md_file, start_line + offset) for offset in range(len(code_lines))
            }
            snippets.append(
                ExtractedSnippet(
                    filename=f"md_{idx}_{md_file.stem}.py",
                    code=code,
                    line_map=line_map,
                )
            )

    if packages_dir.exists():
        for py_file in sorted(packages_dir.glob("*/src/**/*.py")):
            extracted = extract_docstring_snippets(py_file, idx)
            idx += len(extracted)
            snippets.extend(extracted)

    return snippets


def typecheck_documentation(repo_root: Path) -> tuple[int, list[str]]:
    """Extract documentation snippets to a TemporaryDirectory and run mypy on them.

    Returns:
        tuple[int, list[str]]: (snippet_count, error_messages)
    """
    snippets = collect_snippets(repo_root)
    if not snippets:
        return 0, []

    errors: list[str] = []
    default_root = Path(__file__).resolve().parent.parent
    mypy_roots = [r for r in (repo_root, default_root) if (r / "packages").exists()]
    src_paths: list[str] = []
    for root in mypy_roots:
        for src_dir in sorted((root / "packages").glob("*/src")):
            resolved_src = str(src_dir.resolve())
            if resolved_src not in src_paths:
                src_paths.append(resolved_src)

    with tempfile.TemporaryDirectory(prefix="imednet_doc_snippets_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        name_to_map: dict[str, dict[int, tuple[Path, int]]] = {}

        for snippet in snippets:
            try:
                ast.parse(snippet.code, filename=snippet.filename)
            except SyntaxError as exc:
                src_path, src_line = snippet.line_map.get(
                    exc.lineno or 1, (Path(snippet.filename), exc.lineno or 1)
                )
                errors.append(f"{src_path}:{src_line}: error: SyntaxError: {exc.msg}")
                continue

            out_path = tmp_dir / snippet.filename
            out_path.write_text(snippet.code + "\n", encoding="utf-8")
            name_to_map[snippet.filename] = snippet.line_map

        if name_to_map:
            env = os.environ.copy()
            existing_mypypath = env.get("MYPYPATH", "")
            env["MYPYPATH"] = os.pathsep.join(
                [*src_paths, *([existing_mypypath] if existing_mypypath else [])]
            )
            cmd = [
                sys.executable,
                "-m",
                "mypy",
                "--ignore-missing-imports",
                "--no-strict-optional",
                "--allow-untyped-decorators",
                "--check-untyped-defs",
                str(tmp_dir),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if res.returncode != 0:
                for raw_line in res.stdout.splitlines():
                    diag_match = MYPY_DIAG_RE.match(raw_line)
                    if diag_match and diag_match.group(1) in name_to_map:
                        fname = diag_match.group(1)
                        gen_line = int(diag_match.group(2))
                        rest = diag_match.group(3)
                        lmap = name_to_map[fname]
                        src_path, src_line = lmap.get(gen_line, next(iter(lmap.values())))
                        errors.append(f"{src_path}:{src_line}:{rest}")
                    elif raw_line.strip() and not raw_line.startswith("Found "):
                        errors.append(raw_line)
                if not errors and res.stderr.strip():
                    errors.append(res.stderr.strip())

    return len(snippets), errors


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Extract and type-check Python snippets in docs, READMEs, and docstrings."
    )
    parser.add_argument(
        "--root-dir",
        type=Path,
        default=Path("."),
        help="Repository root directory (default: current directory).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for documentation type-checking."""
    args = parse_args(argv)
    count, errors = typecheck_documentation(args.root_dir)
    print(f"Extracted and checked {count} documentation Python snippet unit(s).")

    if errors:
        for err in errors:
            print(err)
        print("Documentation type-check failed.")
        return 1

    print("Documentation type-check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
