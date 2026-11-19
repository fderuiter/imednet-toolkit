"""Extract and strictly type-check Python snippets across docs, READMEs, showcase.js, and examples."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

EXCLUDED_DOC_DIRS = frozenset({"_build", "agents", "superpowers"})
EXCLUDED_DOC_PREFIXES = (
    ("reference", "rest_tables"),
    ("tutorials", "examples", "dummy.py"),
)

RST_DIRECTIVE_RE = re.compile(r"^(\s*)\.\.\s+(testcode|code-block)::(?:\s+([A-Za-z0-9_+-]+))?\s*$")
MD_FENCE_OPEN_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([A-Za-z0-9_+-]+)?(?:\s+.*)?$")
FUTURE_ANNOTATIONS_RE = re.compile(r"^\s*from\s+__future__\s+import\s+annotations\s*$")
SHOWCASE_SDK_BLOCK_RE = re.compile(r"\bsdk\s*:\s*\{(.*?)\n\s*\}", re.DOTALL)
SHOWCASE_ENTRY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*`((?:\\`|[^`])*)`", re.DOTALL)


def _sanitize_segment(segment: str) -> str:
    """Convert a path component into a valid Python module/package identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", segment)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"s_{cleaned}"
    return cleaned


def _mirrored_snippet_path(snippets_dir: Path, rel_path: Path) -> Path:
    """Map a repo-relative source path to a collision-free Python path under snippets_dir."""
    parent_parts = [_sanitize_segment(part) for part in rel_path.parent.parts]
    stem_with_ext = _sanitize_segment(f"{rel_path.stem}_{rel_path.suffix.lstrip('.')}")
    return snippets_dir.joinpath(*parent_parts, f"{stem_with_ext}.py")


def _is_excluded_doc_path(rel_to_docs: Path) -> bool:
    """Return True if a docs-relative path matches excluded archive or generated directories."""
    parts = rel_to_docs.parts
    if EXCLUDED_DOC_DIRS.intersection(parts):
        return True
    return any(parts[: len(prefix)] == prefix for prefix in EXCLUDED_DOC_PREFIXES)


def extract_rst_python_blocks(content: str) -> list[str]:
    """Extract Python snippets from .. testcode:: and .. code-block:: python/py directives."""
    snippets: list[str] = []
    lines = content.splitlines()
    idx = 0
    total = len(lines)

    while idx < total:
        match = RST_DIRECTIVE_RE.match(lines[idx])
        if not match:
            idx += 1
            continue

        directive_indent = len(match.group(1))
        directive_name = match.group(2)
        lang = (match.group(3) or "").lower()
        idx += 1

        if directive_name == "code-block" and lang not in ("python", "py"):
            continue

        # Skip directive options (e.g., :hide:, :caption:, :linenos:) and leading blank lines
        while idx < total:
            curr = lines[idx]
            stripped = curr.strip()
            curr_indent = len(curr) - len(curr.lstrip())
            if not stripped:
                idx += 1
                continue
            if curr_indent > directive_indent and stripped.startswith(":"):
                idx += 1
                continue
            break

        block_lines: list[str] = []
        content_indent: int | None = None
        while idx < total:
            curr = lines[idx]
            if not curr.strip():
                block_lines.append("")
                idx += 1
                continue
            curr_indent = len(curr) - len(curr.lstrip())
            if curr_indent <= directive_indent:
                break
            if content_indent is None:
                content_indent = curr_indent
            block_lines.append(
                curr[content_indent:] if curr_indent >= content_indent else curr.lstrip()
            )
            idx += 1

        dedented = textwrap.dedent("\n".join(block_lines)).strip()
        if dedented:
            snippets.append(dedented)

    return snippets


def extract_markdown_python_blocks(content: str) -> list[str]:
    """Extract fenced ```python and ```py blocks from Markdown text."""
    snippets: list[str] = []
    lines = content.splitlines()
    idx = 0
    total = len(lines)

    while idx < total:
        open_match = MD_FENCE_OPEN_RE.match(lines[idx])
        if not open_match:
            idx += 1
            continue

        fence_indent = len(open_match.group(1))
        fence_marker = open_match.group(2)
        lang = (open_match.group(3) or "").lower()
        idx += 1

        fence_char = fence_marker[0]
        fence_len = len(fence_marker)
        block_lines: list[str] = []

        while idx < total:
            curr = lines[idx]
            stripped = curr.lstrip()
            if (
                stripped.startswith(fence_char * fence_len)
                and not stripped.strip(fence_char).strip()
            ):
                idx += 1
                break
            block_lines.append(curr[fence_indent:] if len(curr) >= fence_indent else curr)
            idx += 1

        if lang in ("python", "py"):
            dedented = textwrap.dedent("\n".join(block_lines)).strip()
            if dedented:
                snippets.append(dedented)

    return snippets


def extract_showcase_js_sdk_templates(content: str) -> dict[str, str]:
    """Extract SNIPPETS.sdk Python templates from docs/_static/showcase.js."""
    sdk_match = SHOWCASE_SDK_BLOCK_RE.search(content)
    if not sdk_match:
        return {}

    templates: dict[str, str] = {}
    for entry_match in SHOWCASE_ENTRY_RE.finditer(sdk_match.group(1)):
        key = entry_match.group(1)
        raw_body = entry_match.group(2)
        decoded = (
            raw_body.replace("\\`", "`")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\'", "'")
            .strip()
        )
        if decoded:
            templates[key] = decoded
    return templates


def _combine_file_snippets(snippets: Sequence[str]) -> str:
    """Combine snippets from a single document, hoisting future annotations if present."""
    has_future = False
    cleaned_blocks: list[str] = []

    for block in snippets:
        kept_lines: list[str] = []
        for line in block.splitlines():
            if FUTURE_ANNOTATIONS_RE.match(line):
                has_future = True
            else:
                kept_lines.append(line)
        joined = "\n".join(kept_lines).strip()
        if joined:
            cleaned_blocks.append(joined)

    header = "from __future__ import annotations\n\n" if has_future else ""
    return header + "\n\n".join(cleaned_blocks) + "\n"


def extract_all_snippets(repo_root: Path, snippets_dir: Path) -> tuple[int, list[Path]]:
    """Extract all documentation snippets into mirrored paths under snippets_dir."""
    if snippets_dir.exists():
        shutil.rmtree(snippets_dir)
    snippets_dir.mkdir(parents=True, exist_ok=True)

    docs_dir = repo_root / "docs"
    written_files: list[Path] = []
    snippet_count = 0

    if docs_dir.is_dir():
        for doc_file in sorted(docs_dir.rglob("*")):
            if not doc_file.is_file():
                continue
            rel_to_docs = doc_file.relative_to(docs_dir)
            if _is_excluded_doc_path(rel_to_docs):
                continue

            if doc_file.suffix == ".rst":
                blocks = extract_rst_python_blocks(doc_file.read_text(encoding="utf-8"))
            elif doc_file.suffix == ".md":
                blocks = extract_markdown_python_blocks(doc_file.read_text(encoding="utf-8"))
            else:
                continue

            if blocks:
                rel_to_repo = doc_file.relative_to(repo_root)
                out_file = _mirrored_snippet_path(snippets_dir, rel_to_repo)
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(_combine_file_snippets(blocks), encoding="utf-8")
                written_files.append(out_file)
                snippet_count += len(blocks)

        showcase_js = docs_dir / "_static" / "showcase.js"
        if showcase_js.is_file():
            templates = extract_showcase_js_sdk_templates(showcase_js.read_text(encoding="utf-8"))
            showcase_dir = snippets_dir / "docs" / "_static" / "showcase_js"
            for key, code in sorted(templates.items()):
                showcase_dir.mkdir(parents=True, exist_ok=True)
                out_file = showcase_dir / f"{_sanitize_segment(key)}.py"
                out_file.write_text(code + "\n", encoding="utf-8")
                written_files.append(out_file)
                snippet_count += 1

    readme_candidates: list[Path] = []
    root_readme = repo_root / "README.md"
    if root_readme.is_file():
        readme_candidates.append(root_readme)
    packages_dir = repo_root / "packages"
    if packages_dir.is_dir():
        readme_candidates.extend(sorted(packages_dir.glob("*/README.md")))

    for md_file in readme_candidates:
        blocks = extract_markdown_python_blocks(md_file.read_text(encoding="utf-8"))
        if blocks:
            rel_to_repo = md_file.relative_to(repo_root)
            out_file = _mirrored_snippet_path(snippets_dir, rel_to_repo)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(_combine_file_snippets(blocks), encoding="utf-8")
            written_files.append(out_file)
            snippet_count += len(blocks)

    return snippet_count, written_files


def run_typecheck(repo_root: Path | None = None, snippets_dir: Path | None = None) -> int:
    """Extract documentation snippets and run strict mypy across snippets and examples/."""
    resolved_root = (repo_root or Path(__file__).resolve().parent.parent).resolve()
    resolved_snippets = (snippets_dir or (resolved_root / "docs" / "_build" / "snippets")).resolve()

    count, written_files = extract_all_snippets(resolved_root, resolved_snippets)
    print(f"Extracted {count} Python snippets across {len(written_files)} mirrored module(s).")

    targets: list[str] = []
    if written_files:
        targets.append(str(resolved_snippets))

    examples_dir = resolved_root / "examples"
    if examples_dir.is_dir():
        targets.append(str(examples_dir))

    if not targets:
        print("No snippets or examples found to type-check.")
        return 0

    pyproject_path = resolved_root / "pyproject.toml"
    cmd = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file",
        str(pyproject_path),
        "--explicit-package-bases",
        "--namespace-packages",
        *targets,
    ]

    mypy_paths = [
        str(resolved_snippets),
        str(resolved_root / "packages" / "core" / "src"),
        str(resolved_root / "packages" / "plugins-workflows" / "src"),
        str(resolved_root / "packages" / "plugins-sinks" / "src"),
        str(resolved_root / "packages" / "plugins-tui" / "src"),
        str(resolved_root / "packages" / "providers-airflow" / "src"),
    ]
    existing_mypypath = os.environ.get("MYPYPATH", "")
    env = os.environ.copy()
    env["MYPYPATH"] = os.pathsep.join([p for p in (*mypy_paths, existing_mypypath) if p])

    print("Running typecheck:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(resolved_root), env=env, capture_output=True, text=True)
    if res.stdout:
        print(res.stdout, end="")
    if res.stderr:
        print(res.stderr, file=sys.stderr, end="")

    if res.returncode != 0:
        print("Documentation and examples type-check failed.")
        return 1

    print("Documentation and examples type-check passed.")
    return 0


def main() -> None:
    """CLI entry point for strict documentation and examples type checking."""
    sys.exit(run_typecheck())


if __name__ == "__main__":
    main()
