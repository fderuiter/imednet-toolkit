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
RST_LITERALINCLUDE_RE = re.compile(r"^(\s*)\.\.\s+literalinclude::\s+(\S+)\s*$")
RST_OPTION_RE = re.compile(r"^:[a-zA-Z0-9_-]+:")
RST_KV_OPTION_RE = re.compile(r"^\s+:([a-zA-Z0-9_-]+):(?:\s+(.*))?$")
MD_FENCE_OPEN_RE = re.compile(r"^(\s*)```(?:python|py)\s*$")
MD_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")
SHOWCASE_SDK_BLOCK_RE = re.compile(r"sdk:\s*\{(.*?)\n\s*\},?\s*rest:", re.DOTALL)
SHOWCASE_SDK_ITEM_RE = re.compile(r"(\w+):\s*`([^`]+)`")
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


def _slice_literalinclude_numbered(
    target_text: str,
    options: dict[str, str],
) -> list[tuple[int, str]] | None:
    """Slice target_text according to Sphinx literalinclude options, preserving 1-based line numbers."""
    if "start-after" in options and "start-at" in options:
        return None
    if "end-before" in options and "end-at" in options:
        return None

    numbered = [(idx + 1, line) for idx, line in enumerate(target_text.splitlines(keepends=True))]

    if "start-after" in options:
        pattern = options["start-after"]
        for idx, (_, line) in enumerate(numbered):
            if pattern in line:
                numbered = numbered[idx + 1 :]
                break
        else:
            return None
    elif "start-at" in options:
        pattern = options["start-at"]
        for idx, (_, line) in enumerate(numbered):
            if pattern in line:
                numbered = numbered[idx:]
                break
        else:
            return None

    if "end-before" in options:
        pattern = options["end-before"]
        for idx, (_, line) in enumerate(numbered):
            if idx > 0 and pattern in line:
                numbered = numbered[:idx]
                break
        else:
            return None
    elif "end-at" in options:
        pattern = options["end-at"]
        for idx, (_, line) in enumerate(numbered):
            if pattern in line:
                numbered = numbered[: idx + 1]
                break
        else:
            return None

    if "lines" in options:
        linespec = options["lines"]
        selected_indices: list[int] = []
        try:
            for raw_item in linespec.split(","):
                item = raw_item.strip()
                if not item:
                    continue
                if "-" in item:
                    if item.count("-") > 1:
                        return None
                    start_str, end_str = item.split("-", 1)
                    if not start_str.strip() and not end_str.strip():
                        return None
                    s = int(start_str) if start_str.strip() else 1
                    e = int(end_str) if end_str.strip() else len(numbered)
                    if s > e:
                        return None
                    for i in range(s - 1, min(e, len(numbered))):
                        if i >= 0 and i not in selected_indices:
                            selected_indices.append(i)
                else:
                    i = int(item) - 1
                    if 0 <= i < len(numbered) and i not in selected_indices:
                        selected_indices.append(i)
        except ValueError:
            return None
        if not selected_indices and numbered:
            return None
        numbered = [numbered[i] for i in selected_indices]

    if "dedent" in options:
        dedent_val = options["dedent"]
        if dedent_val:
            try:
                d = int(dedent_val)
            except ValueError:
                return None
            if d < 0:
                return None
            numbered = [
                (ln, line[d:] if len(line.rstrip("\r\n")) >= d else "\n") for ln, line in numbered
            ]
        else:
            dedented_lines = textwrap.dedent("".join(line for _, line in numbered)).splitlines(
                keepends=True
            )
            numbered = [
                (orig_ln, ded_line)
                for (orig_ln, _), ded_line in zip(numbered, dedented_lines, strict=False)
            ]

    cleaned = [(ln, line.rstrip("\r\n")) for ln, line in numbered]
    while cleaned and cleaned[-1][1] == "":
        cleaned.pop()
    if not any(line.strip() for _, line in cleaned):
        return None
    return cleaned


def extract_rst_literalinclude_snippets(
    text: str,
    rst_file: Path,
    docs_dir: Path,
    counter_start: int,
) -> list[ExtractedSnippet]:
    """Extract Python .. literalinclude:: slices from an RST file."""
    lines = text.splitlines()
    snippets: list[ExtractedSnippet] = []
    idx = counter_start
    n = len(lines)
    i = 0

    while i < n:
        match = RST_LITERALINCLUDE_RE.match(lines[i])
        if not match:
            i += 1
            continue

        target = match.group(2).strip("'\"")
        i += 1
        options: dict[str, str] = {}
        while i < n:
            curr = lines[i]
            opt_match = RST_KV_OPTION_RE.match(curr)
            if opt_match:
                options[opt_match.group(1)] = (opt_match.group(2) or "").strip()
                i += 1
            elif not curr.strip():
                has_next_opt = False
                for next_i in range(i + 1, n):
                    if not lines[next_i].strip():
                        continue
                    if RST_KV_OPTION_RE.match(lines[next_i]):
                        has_next_opt = True
                    break
                if has_next_opt:
                    i += 1
                else:
                    break
            else:
                break

        resolved = (
            (docs_dir / target.lstrip("/")).resolve()
            if target.startswith("/")
            else (rst_file.parent / target).resolve()
        )
        is_python = resolved.suffix == ".py" or options.get("language", "").lower() in (
            "python",
            "py",
        )
        if not is_python or not resolved.is_file():
            continue

        try:
            target_text = resolved.read_text(encoding="utf-8")
        except OSError:
            continue

        sliced = _slice_literalinclude_numbered(target_text, options)
        if not sliced:
            continue

        idx += 1
        code = "\n".join(line for _, line in sliced)
        line_map = {offset + 1: (resolved, orig_ln) for offset, (orig_ln, _) in enumerate(sliced)}
        snippets.append(
            ExtractedSnippet(
                filename=f"rst_li_{idx}_{rst_file.stem}.py",
                code=code,
                line_map=line_map,
            )
        )

    return snippets


def extract_showcase_js_snippets(
    showcase_js_path: Path,
    counter_start: int,
) -> list[ExtractedSnippet]:
    """Extract SNIPPETS.sdk Python templates from docs/_static/showcase.js."""
    if not showcase_js_path.is_file():
        return []
    try:
        content = showcase_js_path.read_text(encoding="utf-8")
    except OSError:
        return []

    sdk_match = SHOWCASE_SDK_BLOCK_RE.search(content)
    if not sdk_match:
        return []

    snippets: list[ExtractedSnippet] = []
    idx = counter_start
    sdk_body = sdk_match.group(1)
    sdk_body_start = sdk_match.start(1)

    for item_match in SHOWCASE_SDK_ITEM_RE.finditer(sdk_body):
        scenario_name = item_match.group(1)
        raw_template = item_match.group(2)
        template_lineno = content[: sdk_body_start + item_match.start(2)].count("\n") + 1
        code = textwrap.dedent(raw_template.replace(r"\n", "\n")).rstrip()
        if not code.strip():
            continue

        idx += 1
        code_lines = code.splitlines()
        line_map = {
            offset + 1: (showcase_js_path, template_lineno) for offset in range(len(code_lines))
        }
        snippets.append(
            ExtractedSnippet(
                filename=f"showcase_sdk_{idx}_{scenario_name}.py",
                code=code,
                line_map=line_map,
            )
        )

    return snippets


def collect_snippets(repo_root: Path) -> list[ExtractedSnippet]:
    """Collect all Python code snippets from RST docs, literalinclude slices, Markdown files, showcase.js, and docstrings."""
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

            li_snippets = extract_rst_literalinclude_snippets(text, rst_file, docs_dir, idx)
            idx += len(li_snippets)
            snippets.extend(li_snippets)

        showcase_snippets = extract_showcase_js_snippets(docs_dir / "_static" / "showcase.js", idx)
        idx += len(showcase_snippets)
        snippets.extend(showcase_snippets)

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
                "--follow-imports=silent",
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
