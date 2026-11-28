"""Validation script for documentation."""

import argparse
import ast
import contextlib
import dataclasses
import functools
import importlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import typing
from collections.abc import Sequence
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml
from defusedxml import ElementTree as DefusedET
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[1]


def check_todos_in_init(
    roots: Sequence[Path] | None = None,
    *,
    repo_root: Path | None = None,
) -> bool:
    """Ensure no TODO strings exist in module, class, or function docstrings across examples/ and packages/*/src/."""
    print("Checking for TODOs in docstrings across examples/ and packages/*/src/...")
    if roots is None:
        base_root = repo_root or REPO_ROOT
        examples_dir = base_root / "examples"
        packages_dir = base_root / "packages"
        if not examples_dir.is_dir() or not packages_dir.is_dir():
            print(
                f"Error: Required directories missing under {base_root} "
                f"(expected 'examples/' and 'packages/')."
            )
            return True
        pkg_srcs = sorted(packages_dir.glob("*/src"))
        if not pkg_srcs:
            print(f"Error: No package 'src/' directories found under {packages_dir}.")
            return True
        candidate_roots = [examples_dir, *pkg_srcs]
    else:
        candidate_roots = list(roots)
        if not candidate_roots:
            print("Error: Empty roots sequence passed to check_todos_in_init.")
            return True

    has_todos = False
    for base_path in candidate_roots:
        if not base_path.exists():
            print(f"Error: Required docstring scan directory not found: {base_path}")
            has_todos = True
            continue
        for py_file in sorted(base_path.rglob("*.py")):
            content = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(content, filename=str(py_file))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    docstring = ast.get_docstring(node, clean=False)
                    if docstring and "TODO" in docstring:
                        lineno = (
                            node.body[0].lineno
                            if getattr(node, "body", None)
                            else getattr(node, "lineno", 1)
                        )
                        target = getattr(node, "name", "<module>")
                        print(f"Error: TODO found in docstring of {target} at {py_file}:{lineno}")
                        has_todos = True

    if not has_todos:
        print("No TODOs found in docstrings.")
    return has_todos


_DOC_EXCLUDED_PARTS = {"_build", "agents", "superpowers"}
_DOCUMENTED_EXTRA_RE = re.compile(
    r"\b(imednet(?:-[a-z0-9]+)*|apache-airflow-providers-imednet)\[([a-z0-9_,\s-]+)\]"
)


def _load_declared_package_extras(repo_root: Path) -> dict[str, set[str]]:
    """Load declared [project.optional-dependencies] keys keyed by package name."""
    pkg_extras: dict[str, set[str]] = {}
    pyproject_files = [
        repo_root / "pyproject.toml",
        *sorted((repo_root / "packages").glob("*/pyproject.toml")),
    ]
    for pyproject_path in pyproject_files:
        if not pyproject_path.is_file():
            continue
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        pkg_name = project.get("name")
        if isinstance(pkg_name, str):
            opt_deps = project.get("optional-dependencies", {})
            pkg_extras[pkg_name] = set(opt_deps.keys()) if isinstance(opt_deps, dict) else set()
    return pkg_extras


def check_documented_extras(repo_root: Path | None = None) -> bool:
    """Validate that every documented <package>[<extra>] reference matches a declared extra in pyproject.toml."""
    print("Checking documented package extras against pyproject.toml declarations...")
    root = repo_root or REPO_ROOT
    pkg_extras = _load_declared_package_extras(root)
    if not pkg_extras:
        print(f"Error: No pyproject.toml package definitions found under {root}.")
        return True

    scan_candidates: list[Path] = []
    root_readme = root / "README.md"
    if root_readme.is_file():
        scan_candidates.append(root_readme)
    packages_dir = root / "packages"
    if packages_dir.exists():
        scan_candidates.extend(sorted(packages_dir.glob("*/README.md")))
        scan_candidates.extend(sorted(packages_dir.glob("*/src/**/*.py")))
    docs_dir = root / "docs"
    if docs_dir.exists():
        scan_candidates.extend(sorted(docs_dir.rglob("*.rst")))
        scan_candidates.extend(sorted(docs_dir.rglob("*.md")))
    examples_dir = root / "examples"
    if examples_dir.exists():
        scan_candidates.extend(sorted(examples_dir.rglob("*.py")))
        scan_candidates.extend(sorted(examples_dir.rglob("*.md")))

    if not scan_candidates:
        print(f"Error: No documentation or source files found to scan under {root}.")
        return True

    has_errors = False
    seen: set[Path] = set()
    for path in scan_candidates:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        rel_parts = path.relative_to(root).parts
        if any(part in _DOC_EXCLUDED_PARTS for part in rel_parts):
            continue

        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _DOCUMENTED_EXTRA_RE.finditer(line):
                pkg_name = match.group(1)
                extras_raw = match.group(2)
                if pkg_name not in pkg_extras:
                    print(
                        f"Error: Unknown workspace package '{pkg_name}' in extra reference at {path}:{lineno}"
                    )
                    has_errors = True
                    continue
                declared = pkg_extras[pkg_name]
                for extra in (item.strip() for item in extras_raw.split(",") if item.strip()):
                    if extra not in declared:
                        print(
                            f"Error: Undeclared extra '{pkg_name}[{extra}]' at {path}:{lineno} "
                            f"(declared extras for {pkg_name}: {sorted(declared)})"
                        )
                        has_errors = True

    if not has_errors:
        print("All documented package extras match pyproject.toml declarations.")
    return has_errors


class _CLIValidationError(Exception):
    """Raised when an argparse parser rejects a documented CLI command."""


def _patch_cli_parser_errors(parser: argparse.ArgumentParser) -> None:
    """Patch parser.error recursively on parser and all subparsers to raise _CLIValidationError."""

    def _raise_error(message: str) -> Any:
        raise _CLIValidationError(message)

    parser.error = _raise_error  # type: ignore[method-assign]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                _patch_cli_parser_errors(subparser)


def _extract_shell_code_blocks(path: Path) -> list[tuple[int, str]]:
    """Extract (1-based start line, shell block text) from Markdown or RST files."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[int, str]] = []
    n = len(lines)

    if path.suffix == ".md":
        i = 0
        while i < n:
            if re.match(r"^\s*```(?:bash|sh|shell|console|zsh)\s*$", lines[i]):
                start_line = i + 2
                i += 1
                buf: list[str] = []
                while i < n and not re.match(r"^\s*```\s*$", lines[i]):
                    buf.append(lines[i])
                    i += 1
                blocks.append((start_line, "\n".join(buf)))
            i += 1
    elif path.suffix == ".rst":
        i = 0
        while i < n:
            match = re.match(
                r"^(\s*)\.\.\s+(?:code-block|sourcecode|prompt)::\s*(?:bash|sh|shell|console|zsh)\b",
                lines[i],
            )
            if match:
                base_indent = len(match.group(1))
                i += 1
                while i < n:
                    stripped = lines[i].strip()
                    if stripped == "":
                        i += 1
                        break
                    if (len(lines[i]) - len(lines[i].lstrip())) > base_indent and re.match(
                        r"^:[a-zA-Z0-9_-]+:", stripped
                    ):
                        i += 1
                    else:
                        break
                while i < n and lines[i].strip() == "":
                    i += 1
                if i < n:
                    indent = len(lines[i]) - len(lines[i].lstrip())
                    if indent > base_indent:
                        start_line = i + 1
                        buf = []
                        while i < n:
                            if lines[i].strip() == "":
                                buf.append("")
                            elif (len(lines[i]) - len(lines[i].lstrip())) >= indent:
                                buf.append(lines[i][indent:])
                            else:
                                break
                            i += 1
                        blocks.append((start_line, "\n".join(buf)))
                        continue
            i += 1

    return blocks


def _split_shell_pipeline(cmd: str) -> list[list[str]]:
    """Split a shell command line into individual command token lists while preserving <PLACEHOLDER> args."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars="|&;")
    lex.whitespace_split = True
    tokens = list(lex)
    segments: list[list[str]] = []
    curr: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in ("|", "||", "&&", ";"):
            if curr:
                segments.append(curr)
                curr = []
        elif tok in (">", ">>", "2>", "2>&1", "&>"):
            skip_next = tok != "2>&1"
        else:
            curr.append(tok)
    if curr:
        segments.append(curr)
    return segments


def check_cli_snippets(repo_root: Path | None = None) -> bool:
    """Parse documented `imednet ...` CLI invocations against imednet.cli.get_parser()."""
    print("Checking documented imednet CLI commands against CLI argument parser...")
    root_dir = repo_root or REPO_ROOT
    wheel_mode = os.environ.get("IMEDNET_TEST_WHEELS") == "1"
    for root in (root_dir, REPO_ROOT):
        if wheel_mode and root.resolve() == REPO_ROOT.resolve():
            continue
        for pkg_sub in (
            "packages/core/src",
            "packages/plugins-workflows/src",
            "packages/plugins-sinks/src",
            "packages/plugins-tui/src",
            "packages/providers-airflow/src",
        ):
            src_dir = (root / pkg_sub).resolve()
            if src_dir.is_dir() and str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

    from imednet.cli import get_parser

    parser = get_parser()
    _patch_cli_parser_errors(parser)

    candidates: list[Path] = []
    root_readme = root_dir / "README.md"
    if root_readme.is_file():
        candidates.append(root_readme)
    packages_dir = root_dir / "packages"
    if packages_dir.exists():
        candidates.extend(sorted(packages_dir.glob("*/README.md")))
    docs_dir = root_dir / "docs"
    if docs_dir.exists():
        candidates.extend(sorted(docs_dir.rglob("*.rst")))
        candidates.extend(sorted(docs_dir.rglob("*.md")))
    examples_dir = root_dir / "examples"
    if examples_dir.exists():
        candidates.extend(sorted(examples_dir.rglob("*.md")))

    if not candidates:
        print(f"Error: No documentation files found for CLI validation under {root_dir}.")
        return True

    has_errors = False
    checked_count = 0
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        rel_parts = path.relative_to(root_dir).parts
        if any(part in _DOC_EXCLUDED_PARTS for part in rel_parts):
            continue

        for start_line, block in _extract_shell_code_blocks(path):
            logical_lines: list[tuple[int, str]] = []
            acc = ""
            acc_line = start_line
            for offset, raw_line in enumerate(block.splitlines()):
                stripped = raw_line.strip()
                if not acc:
                    acc_line = start_line + offset
                if stripped.endswith("\\") and not stripped.startswith("#"):
                    acc += stripped[:-1] + " "
                else:
                    acc += stripped
                    logical_lines.append((acc_line, acc.strip()))
                    acc = ""
            if acc:
                logical_lines.append((acc_line, acc.strip()))

            for lineno, cmd_line in logical_lines:
                if not cmd_line or cmd_line.startswith("#"):
                    continue
                normalized_cmd = cmd_line[2:].strip() if cmd_line.startswith("$ ") else cmd_line

                try:
                    segments = _split_shell_pipeline(normalized_cmd)
                except ValueError as exc:
                    if "imednet" in normalized_cmd:
                        print(f"Error: Malformed shell command at {path}:{lineno}: {exc}")
                        has_errors = True
                    continue

                for seg in segments:
                    cmd_tokens = list(seg)
                    while cmd_tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", cmd_tokens[0]):
                        cmd_tokens = cmd_tokens[1:]
                    if cmd_tokens[:2] == ["uv", "run"]:
                        cmd_tokens = cmd_tokens[2:]
                    if cmd_tokens and cmd_tokens[0] == "imednet":
                        checked_count += 1
                        parsed_args: argparse.Namespace | None = None
                        try:
                            with (
                                contextlib.redirect_stdout(io.StringIO()),
                                contextlib.redirect_stderr(io.StringIO()),
                            ):
                                parsed_args = parser.parse_args(cmd_tokens[1:])
                        except SystemExit as exc:
                            if exc.code not in (0, None):
                                print(
                                    f"Error: Invalid CLI command '{' '.join(cmd_tokens)}' at {path}:{lineno} "
                                    f"(exited with code {exc.code})"
                                )
                                has_errors = True
                            continue
                        except _CLIValidationError as exc:
                            print(
                                f"Error: Invalid CLI command '{' '.join(cmd_tokens)}' at {path}:{lineno}: {exc}"
                            )
                            has_errors = True
                            continue

                        if (
                            parsed_args is not None
                            and "--simulation" in cmd_tokens[1:]
                            and getattr(parsed_args, "command", None) != "tui"
                            and "--interactive" not in cmd_tokens[1:]
                        ):
                            from imednet.cli import app as cli_app
                            from imednet.simulation.state import reset_default_simulation_state

                            placeholder_map = {
                                "<STUDY_KEY>": "STUDY",
                                "<SUBJECT_KEY>": "SUB1",
                            }
                            exec_args = [placeholder_map.get(t, t) for t in cmd_tokens[1:]]
                            prev_cwd = Path.cwd()
                            prev_non_interactive = os.environ.get("IMEDNET_NON_INTERACTIVE")
                            out_buf = io.StringIO()
                            err_buf = io.StringIO()
                            reset_default_simulation_state()
                            try:
                                with tempfile.TemporaryDirectory() as tmp_dir:
                                    os.chdir(tmp_dir)
                                    os.environ["IMEDNET_NON_INTERACTIVE"] = "1"
                                    with (
                                        contextlib.redirect_stdout(out_buf),
                                        contextlib.redirect_stderr(err_buf),
                                    ):
                                        cli_app(exec_args)
                            except SystemExit as exc:
                                if exc.code not in (0, None):
                                    err_text = err_buf.getvalue().strip()
                                    print(
                                        f"Error: Simulation CLI command '{' '.join(cmd_tokens)}' at {path}:{lineno} "
                                        f"exited with code {exc.code}"
                                        + (f": {err_text}" if err_text else "")
                                    )
                                    has_errors = True
                            except Exception as exc:
                                print(
                                    f"Error: Simulation CLI command '{' '.join(cmd_tokens)}' at {path}:{lineno} "
                                    f"failed: {exc}"
                                )
                                has_errors = True
                            finally:
                                reset_default_simulation_state()
                                os.chdir(prev_cwd)
                                if prev_non_interactive is None:
                                    os.environ.pop("IMEDNET_NON_INTERACTIVE", None)
                                else:
                                    os.environ["IMEDNET_NON_INTERACTIVE"] = prev_non_interactive

    if not has_errors:
        print(f"CLI command validation passed ({checked_count} command(s) verified).")
    return has_errors


def check_packages_registered(repo_root: Path | None = None) -> bool:
    """Validate that all packages in /packages are registered across workspace and docs configs."""
    print("Checking if all packages are registered in build configuration...")
    root = repo_root or REPO_ROOT

    packages_dir = root / "packages"
    if not packages_dir.is_dir():
        print(f"Error: Path {packages_dir} not found.")
        return True

    conf_py_path = root / "docs" / "conf.py"
    index_rst_path = root / "docs" / "index.rst"
    plugins_rst_path = root / "docs" / "explanation" / "plugins.rst"
    cli_rst_path = root / "docs" / "how-to" / "cli.rst"
    pyproject_path = root / "pyproject.toml"
    readme_path = root / "README.md"

    for required_file in (
        conf_py_path,
        index_rst_path,
        plugins_rst_path,
        cli_rst_path,
        pyproject_path,
        readme_path,
    ):
        if not required_file.is_file():
            print(f"Error: Required configuration/doc file not found: {required_file}")
            return True

    conf_py_content = conf_py_path.read_text(encoding="utf-8")
    index_rst_content = index_rst_path.read_text(encoding="utf-8")
    plugins_rst_content = (
        plugins_rst_path.read_text(encoding="utf-8") if plugins_rst_path.exists() else ""
    )

    has_errors = False

    for pkg_dir in sorted(packages_dir.iterdir()):
        if not pkg_dir.is_dir() or pkg_dir.name.startswith("."):
            continue

        pkg_name = pkg_dir.name
        src_path = f"../packages/{pkg_name}/src"

        # Check docs/conf.py
        expected_path_format_1 = src_path
        expected_path_format_2 = f'"{pkg_name}" / "src"'
        if (
            expected_path_format_1 not in conf_py_content
            and expected_path_format_2 not in conf_py_content
        ):
            print(f"Error: Package {pkg_name} is missing from docs/conf.py sys.path.")
            has_errors = True

        # Check docs/index.rst
        index_pkg_path = f"reference/api/{pkg_name}"
        if index_pkg_path not in index_rst_content:
            print(
                f"Error: Package {pkg_name} ({index_pkg_path}) is missing from docs/index.rst API Reference."
            )
            has_errors = True

        # Check docs/explanation/plugins.rst
        plugins_doc_ref = f"/reference/api/{pkg_name}"
        if plugins_rst_path.exists() and plugins_doc_ref not in plugins_rst_content:
            print(
                f"Error: Package {pkg_name} ({plugins_doc_ref}) is missing from docs/explanation/plugins.rst."
            )
            has_errors = True

    if plugins_rst_path.exists() and "imednet.cli_plugins" not in plugins_rst_content:
        print(
            "Error: imednet.cli_plugins entry-point group is missing from docs/explanation/plugins.rst."
        )
        has_errors = True

    if pyproject_path.exists():
        pyproject_data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        root_deps = set(pyproject_data.get("project", {}).get("dependencies", []))
        for required_dep in (
            "imednet",
            "imednet-workflows",
            "apache-airflow-providers-imednet",
            "imednet-plugins-sinks",
            "imednet-tui",
        ):
            if required_dep not in root_deps:
                print(
                    f"Error: '{required_dep}' is missing from root pyproject.toml [project].dependencies."
                )
                has_errors = True

    if readme_path.exists():
        readme_content = readme_path.read_text(encoding="utf-8")
        for required_pkg in (
            "imednet",
            "imednet-workflows",
            "apache-airflow-providers-imednet",
            "imednet-plugins-sinks",
            "imednet-tui",
        ):
            if required_pkg not in readme_content:
                print(
                    f"Error: '{required_pkg}' is missing from README.md installation instructions."
                )
                has_errors = True
        if "uv sync --locked --all-packages --all-extras" not in readme_content:
            print(
                "Error: 'uv sync --locked --all-packages --all-extras' is missing from README.md."
            )
            has_errors = True

    if cli_rst_path.exists():
        cli_rst_content = cli_rst_path.read_text(encoding="utf-8")
        for required_token in (
            "imednet tui",
            "--high-contrast",
            "InteractiveDataGrid",
            "IMEDNET_NON_INTERACTIVE=1",
            "--interactive",
        ):
            if required_token not in cli_rst_content:
                print(f"Error: '{required_token}' is missing from docs/how-to/cli.rst.")
                has_errors = True

    if not has_errors:
        print("All packages are correctly registered.")
    return has_errors


_MD_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_MD_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*$")
_MD_LINK_RE = re.compile(
    r"""(?<!!)\[(?:[^\[\]]|`[^`]*`)*\]\(\s*([^)\s>]+|<[^>]+>)(?:\s+["'][^"']*["'])?\s*\)"""
)
_MD_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
_INLINE_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_GITHUB_PAGES_BASE = "https://fderuiter.github.io/imednet-toolkit/"
_SOURCE_SECTION_KEYWORDS = (
    "source checkout",
    "source install",
    "from source",
    "editable",
    "developer quickstart",
)
_PIP_REQ_TOKEN_RE = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[([A-Za-z0-9_,\s-]+)\])?")

REQUIRED_PYTHON_CLASSIFIERS = (
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
)
WORKSPACE_PACKAGE_IDS = (
    "core",
    "plugins-workflows",
    "plugins-sinks",
    "plugins-tui",
    "providers-airflow",
)
LIVE_GITHUB_AREA_NAMES = frozenset(
    {
        "core",
        "http",
        "models",
        "auth",
        "pagination",
        "cli",
        "workflows",
        "airflow",
        "docs",
        "tests",
        "release",
        "github",
        "ci",
        "testing",
        "streamlit",
        "mapping",
        "records",
        "standards",
        "dashboard",
        "review-workbench",
        "terminology",
        "governance",
        "performance",
    }
)
RETIRED_GITHUB_LABELS = (
    "type:maintenance",
    "area:workflows-plugin",
    "area:airflow-provider",
    "area:contributing",
    "status:needs-info",
    "risk:medium",
    "risk:high",
)
RETIRED_REPO_SLUG = "imednet-python-sdk"
OBSOLETE_CHANGELOG_TOKENS = (
    "docs/contributing.rst",
    "imednet.compat.v1",
    "imednet-python-sdk",
)


def _extract_markdown_links(path: Path) -> list[tuple[int, str]]:
    """Extract (1-based line number, link target) outside fenced code blocks in a Markdown file."""
    links: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _MD_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _MD_LINK_RE.finditer(line):
            raw_target = match.group(1).strip()
            if raw_target.startswith("<") and raw_target.endswith(">"):
                raw_target = raw_target[1:-1].strip()
            if raw_target:
                links.append((lineno, raw_target))
        scrubbed_inline = _INLINE_CODE_SPAN_RE.sub("", line)
        scrubbed_links = _MD_LINK_RE.sub("", scrubbed_inline)
        for auto_match in _MD_AUTOLINK_RE.finditer(scrubbed_links):
            links.append((lineno, auto_match.group(1).strip()))
    return links


class _HTMLAnchorCollector(HTMLParser):
    """Collect id and name anchor targets from rendered HTML files."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        for attr_name, attr_val in attrs:
            if attr_name in ("id", "name") and attr_val:
                self.anchors.add(attr_val)


def check_package_docs(repo_root: Path | None = None) -> bool:
    """Validate package README/CHANGELOG files, wheel manifest metadata, absolute links, and MyST changelog hub."""
    print(
        "Checking package local documentation, manifest metadata, absolute links, root CHANGELOG.md, and MyST includes..."
    )
    root = repo_root or REPO_ROOT
    packages_dir = root / "packages"
    if not packages_dir.is_dir():
        print(f"Error: Path {packages_dir} not found.")
        return True

    root_changelog = root / "CHANGELOG.md"
    if not root_changelog.is_file():
        print(f"Error: Root CHANGELOG.md not found at {root_changelog}.")
        return True

    root_changelog_content = root_changelog.read_text(encoding="utf-8")
    has_errors = False

    for token in OBSOLETE_CHANGELOG_TOKENS:
        if token in root_changelog_content:
            print(f"Error: Root CHANGELOG.md contains obsolete reference '{token}'.")
            has_errors = True
    if re.search(r"\bctx\b", root_changelog_content):
        print("Error: Root CHANGELOG.md contains stale 'ctx' deprecation claims.")
        has_errors = True

    pkg_dirs = [
        p for p in sorted(packages_dir.iterdir()) if p.is_dir() and not p.name.startswith(".")
    ]
    if not pkg_dirs:
        print(f"Error: No workspace packages found under {packages_dir}.")
        return True

    changelogs_hub = root / "docs" / "reference" / "changelogs" / "index.rst"
    compatibility_rst = root / "docs" / "reference" / "compatibility.rst"
    contributing_rst = root / "docs" / "reference" / "contributing.rst"
    if not changelogs_hub.is_file():
        print(f"Error: Missing Sphinx changelog hub at {changelogs_hub}.")
        has_errors = True
    else:
        hub_text = changelogs_hub.read_text(encoding="utf-8")
        for pkg_dir in pkg_dirs:
            pkg_name = pkg_dir.name
            wrapper_md = root / "docs" / "reference" / "changelogs" / f"{pkg_name}.md"
            if not wrapper_md.is_file():
                print(f"Error: Missing MyST changelog wrapper {wrapper_md}.")
                has_errors = True
            else:
                wrapper_text = wrapper_md.read_text(encoding="utf-8")
                expected_include = f"packages/{pkg_name}/CHANGELOG.md"
                if "{include}" not in wrapper_text or expected_include not in wrapper_text:
                    print(
                        f"Error: {wrapper_md} must include '{expected_include}' via MyST {{include}}."
                    )
                    has_errors = True
            if pkg_name not in hub_text:
                print(f"Error: {changelogs_hub} does not index '{pkg_name}'.")
                has_errors = True

    for ref_doc in (compatibility_rst, contributing_rst):
        if not ref_doc.is_file():
            print(f"Error: Missing reference document {ref_doc}.")
            has_errors = True
        elif "changelogs/index" not in ref_doc.read_text(encoding="utf-8"):
            print(f"Error: {ref_doc} must link to 'changelogs/index'.")
            has_errors = True

    for pkg_dir in pkg_dirs:
        pkg_name = pkg_dir.name

        readme_path = pkg_dir / "README.md"
        if not readme_path.is_file():
            print(f"Error: Package {pkg_name} is missing a local README.md.")
            has_errors = True
        else:
            for lineno, target in _extract_markdown_links(readme_path):
                base_target = target.split("#", 1)[0]
                if base_target.endswith(".rst"):
                    print(
                        f"Error: Raw .rst link '{target}' found in {readme_path}:{lineno}; "
                        f"use rendered {_GITHUB_PAGES_BASE} HTML URLs."
                    )
                    has_errors = True
                elif not target.startswith(("https://", "mailto:", "#")):
                    print(
                        f"Error: Relative or non-HTTPS link '{target}' found in {readme_path}:{lineno}; "
                        "package READMEs must use absolute https:// URLs for PyPI rendering."
                    )
                    has_errors = True

        changelog_path = pkg_dir / "CHANGELOG.md"
        if not changelog_path.is_file():
            print(f"Error: Package {pkg_name} is missing a local CHANGELOG.md.")
            has_errors = True

        if f"packages/{pkg_name}/CHANGELOG.md" not in root_changelog_content:
            print(
                f"Error: Package changelog 'packages/{pkg_name}/CHANGELOG.md' is not linked in root CHANGELOG.md."
            )
            has_errors = True

        # Check pyproject.toml wheel metadata fields
        manifest_path = pkg_dir / "pyproject.toml"
        if not manifest_path.is_file():
            print(f"Error: Package {pkg_name} is missing pyproject.toml.")
            has_errors = True
            continue

        manifest_data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        project = manifest_data.get("project", {})

        readme_decl = project.get("readme")
        readme_rel = (
            readme_decl
            if isinstance(readme_decl, str)
            else readme_decl.get("file")
            if isinstance(readme_decl, dict)
            else None
        )
        if not isinstance(readme_rel, str) or not readme_rel.strip():
            print(f"Error: {manifest_path} is missing [project].readme.")
            has_errors = True
        else:
            declared_readme = pkg_dir / readme_rel.strip()
            if (
                not declared_readme.is_file()
                or not declared_readme.read_text(encoding="utf-8").strip()
            ):
                print(
                    f"Error: {manifest_path} [project].readme points to missing or empty file '{readme_rel}'."
                )
                has_errors = True

        license_field = project.get("license")
        license_val = (
            license_field.strip()
            if isinstance(license_field, str)
            else license_field.get("text", "").strip()
            if isinstance(license_field, dict) and isinstance(license_field.get("text"), str)
            else ""
        )
        if license_val != "MIT":
            print(f"Error: {manifest_path} must declare license = \"MIT\" (got {license_field!r}).")
            has_errors = True

        classifiers = set(project.get("classifiers", []))
        for required_classifier in REQUIRED_PYTHON_CLASSIFIERS:
            if required_classifier not in classifiers:
                print(f"Error: {manifest_path} is missing classifier '{required_classifier}'.")
                has_errors = True

    if not has_errors:
        print("All package docs, MyST changelogs, and manifest metadata checks passed.")
    return has_errors


def check_governance_and_issue_templates(repo_root: Path | None = None) -> bool:
    """Validate governance URLs, live GitHub triage labels, issue templates, and README package/extra listings."""
    print("Checking governance files, issue templates, and triage label vocabulary...")
    root = repo_root or REPO_ROOT
    issue_template_dir = root / ".github" / "ISSUE_TEMPLATE"
    docs_dir = root / "docs"
    if not issue_template_dir.is_dir() or not docs_dir.is_dir():
        print(
            f"Error: Required governance directories missing under {root} "
            f"(expected '.github/ISSUE_TEMPLATE/' and 'docs/')."
        )
        return True

    has_errors = False

    url_scan_files: list[Path] = []
    for root_file in (
        "README.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "AGENTS.md",
        "CHANGELOG.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ):
        candidate = root / root_file
        if candidate.is_file():
            url_scan_files.append(candidate)

    url_scan_files.extend(sorted(p for p in issue_template_dir.iterdir() if p.is_file()))

    packages_dir = root / "packages"
    if packages_dir.is_dir():
        url_scan_files.extend(sorted(packages_dir.glob("*/pyproject.toml")))
        url_scan_files.extend(sorted(packages_dir.glob("*/README.md")))

    public_doc_files: list[Path] = []
    for doc_path in sorted([*docs_dir.rglob("*.rst"), *docs_dir.rglob("*.md")]):
        rel_parts = doc_path.relative_to(root).parts
        if any(part in _DOC_EXCLUDED_PARTS for part in rel_parts):
            continue
        public_doc_files.append(doc_path)

    seen_url_files: set[Path] = set()
    for path in [*url_scan_files, *public_doc_files]:
        resolved = path.resolve()
        if resolved in seen_url_files or not path.is_file():
            continue
        seen_url_files.add(resolved)
        text = path.read_text(encoding="utf-8")
        if RETIRED_REPO_SLUG in text:
            print(
                f"Error: Retired repository slug '{RETIRED_REPO_SLUG}' found in {path.relative_to(root)}."
            )
            has_errors = True

    label_scan_files = [
        *public_doc_files,
        *sorted(p for p in issue_template_dir.iterdir() if p.is_file()),
    ]

    label_patterns = {
        label: re.compile(rf"(?<![a-z0-9:-]){re.escape(label)}(?![a-z0-9-])")
        for label in RETIRED_GITHUB_LABELS
    }
    for path in label_scan_files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in label_patterns.items():
            if pattern.search(text):
                print(
                    f"Error: Nonexistent GitHub label '{label}' found in {path.relative_to(root)}."
                )
                has_errors = True

    for yml_path in sorted(issue_template_dir.glob("*.yml")):
        if yml_path.name == "config.yml":
            continue
        data = yaml.safe_load(yml_path.read_text(encoding="utf-8")) or {}
        body = data.get("body", [])
        fields_by_id = {
            item.get("id"): item
            for item in body
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

        area_field = fields_by_id.get("area")
        area_options = (
            area_field.get("attributes", {}).get("options", [])
            if isinstance(area_field, dict)
            else []
        )
        if not area_options:
            print(
                f"Error: Issue template {yml_path.relative_to(root)} is missing 'area' dropdown options."
            )
            has_errors = True
        else:
            invalid_areas = [opt for opt in area_options if str(opt) not in LIVE_GITHUB_AREA_NAMES]
            if invalid_areas:
                print(
                    f"Error: Issue template {yml_path.relative_to(root)} contains non-live area options: {invalid_areas}."
                )
                has_errors = True

        pkg_field = fields_by_id.get("package")
        pkg_options = (
            {
                str(opt).split(" ", 1)[0]
                for opt in pkg_field.get("attributes", {}).get("options", [])
            }
            if isinstance(pkg_field, dict)
            else set()
        )
        missing_pkgs = [pkg for pkg in WORKSPACE_PACKAGE_IDS if pkg not in pkg_options]
        if missing_pkgs:
            print(
                f"Error: Issue template {yml_path.relative_to(root)} 'package' dropdown is missing workspace packages: {missing_pkgs}."
            )
            has_errors = True

    root_readme = root / "README.md"
    core_manifest = root / "packages" / "core" / "pyproject.toml"
    if root_readme.is_file() and core_manifest.is_file():
        readme_text = root_readme.read_text(encoding="utf-8")
        core_data = tomllib.loads(core_manifest.read_text(encoding="utf-8"))
        core_extras = sorted(core_data.get("project", {}).get("optional-dependencies", {}).keys())
        for extra in core_extras:
            if not re.search(rf"\b{re.escape(extra)}\b", readme_text):
                print(f"Error: Core extra '{extra}' is not documented in root README.md.")
                has_errors = True
        for pkg_dist in ("imednet-plugins-sinks", "imednet-tui"):
            if pkg_dist not in readme_text:
                print(f"Error: Workspace package '{pkg_dist}' is missing from root README.md.")
                has_errors = True

    core_readme = root / "packages" / "core" / "README.md"
    if core_readme.is_file():
        core_readme_text = core_readme.read_text(encoding="utf-8")
        for pkg_dist in ("imednet-plugins-sinks", "imednet-tui"):
            if pkg_dist not in core_readme_text:
                print(
                    f"Error: Workspace package '{pkg_dist}' is missing from packages/core/README.md."
                )
                has_errors = True

    if not has_errors:
        print("Governance, issue template, and triage label checks passed.")
    return has_errors


def check_readme_github_pages_routes(
    html_dir: Path | None = None,
    *,
    require_exists: bool = False,
    repo_root: Path | None = None,
) -> bool:
    """Verify zero raw .rst/docs links in READMEs and resolve all GitHub Pages URLs and #fragments."""
    print("Checking README GitHub Pages routes and #fragment anchors...")
    root = repo_root or REPO_ROOT
    readme_files: list[Path] = []
    root_readme = root / "README.md"
    if root_readme.is_file():
        readme_files.append(root_readme)
    packages_dir = root / "packages"
    if packages_dir.is_dir():
        readme_files.extend(sorted(packages_dir.glob("*/README.md")))

    if not readme_files:
        print(f"Error: No README.md files found under {root}.")
        return True

    has_errors = False
    pages_links: list[tuple[Path, int, str]] = []

    for readme_path in readme_files:
        for lineno, target in _extract_markdown_links(readme_path):
            base_part = target.split("#", 1)[0]
            if base_part.endswith(".rst"):
                print(
                    f"Error: Raw .rst link '{target}' in {readme_path}:{lineno}; "
                    f"replace with a rendered {_GITHUB_PAGES_BASE} HTML URL."
                )
                has_errors = True
                continue
            if base_part == "docs" or base_part.startswith(("docs/", "./docs/", "../docs/")):
                print(
                    f"Error: Relative docs/ link '{target}' in {readme_path}:{lineno}; "
                    f"replace with a rendered {_GITHUB_PAGES_BASE} HTML URL."
                )
                has_errors = True
                continue
            if target.startswith("http://fderuiter.github.io/"):
                print(f"Error: Non-HTTPS GitHub Pages URL '{target}' in {readme_path}:{lineno}.")
                has_errors = True
                continue
            if target == _GITHUB_PAGES_BASE.rstrip("/"):
                print(
                    f"Error: GitHub Pages URL '{target}' in {readme_path}:{lineno} is missing trailing slash."
                )
                has_errors = True
                continue
            if target.startswith(_GITHUB_PAGES_BASE):
                pages_links.append((readme_path, lineno, target))

    if html_dir is None:
        if not has_errors:
            print(
                f"README route syntax check passed ({len(pages_links)} GitHub Pages URL(s), 0 raw .rst links)."
            )
        return has_errors

    if not html_dir.is_dir():
        if require_exists:
            print(f"Error: Rendered HTML directory {html_dir} does not exist.")
            return True
        if not has_errors:
            print(
                f"Skipping HTML route target resolution ({html_dir} not built); 0 raw .rst links found."
            )
        return has_errors

    anchor_cache: dict[Path, set[str]] = {}
    for readme_path, lineno, url in pages_links:
        rel_spec = url[len(_GITHUB_PAGES_BASE) :]
        if "#" in rel_spec:
            rel_path_str, fragment = rel_spec.split("#", 1)
            if not fragment:
                print(f"Error: Empty '#' fragment in '{url}' at {readme_path}:{lineno}.")
                has_errors = True
                continue
        else:
            rel_path_str, fragment = rel_spec, None

        if rel_path_str == "":
            target_html = html_dir / "index.html"
        elif rel_path_str.endswith("/"):
            target_html = html_dir / rel_path_str.rstrip("/") / "index.html"
        else:
            target_html = html_dir / rel_path_str

        if not target_html.is_file():
            print(
                f"Error: Broken GitHub Pages URL '{url}' at {readme_path}:{lineno} "
                f"(expected {target_html} in {html_dir})."
            )
            has_errors = True
            continue

        if fragment is not None:
            resolved_html = target_html.resolve()
            if resolved_html not in anchor_cache:
                collector = _HTMLAnchorCollector()
                collector.feed(resolved_html.read_text(encoding="utf-8"))
                anchor_cache[resolved_html] = collector.anchors
            if fragment not in anchor_cache[resolved_html]:
                print(
                    f"Error: Missing HTML fragment '#{fragment}' for '{url}' at {readme_path}:{lineno} "
                    f"(not found in {target_html.relative_to(html_dir)})."
                )
                has_errors = True

    if not has_errors:
        print(
            f"README GitHub Pages route check passed ({len(pages_links)} URL(s) and fragments resolved in {html_dir})."
        )
    return has_errors


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _discover_published_tag_extras(
    repo_root: Path,
) -> tuple[dict[str, str], dict[str, set[str]], set[str]] | None:
    """Discover latest <package>-v* git tags and their declared extras for workspace packages."""
    packages_dir = repo_root / "packages"
    if not packages_dir.is_dir():
        return {}, {}, set()

    workspace_manifests: dict[str, str] = {}
    for pyproject_path in sorted(packages_dir.glob("*/pyproject.toml")):
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        raw_name = data.get("project", {}).get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            norm_name = _normalize_dist_name(raw_name)
            workspace_manifests[norm_name] = pyproject_path.relative_to(repo_root).as_posix()

    if not workspace_manifests:
        return {}, {}, set()

    tag_proc = subprocess.run(
        ["git", "-C", str(repo_root), "tag", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    if tag_proc.returncode != 0:
        print(f"Warning: Unable to list git tags in {repo_root}: {tag_proc.stderr.strip()}")
        return None

    all_tags = [line.strip() for line in tag_proc.stdout.splitlines() if line.strip()]
    latest_tags: dict[str, tuple[Version, str]] = {}
    for tag in all_tags:
        for pkg_name in workspace_manifests:
            prefix = f"{pkg_name}-v"
            if tag.startswith(prefix):
                ver_str = tag[len(prefix) :]
                try:
                    parsed_ver = Version(ver_str)
                except InvalidVersion:
                    continue
                current = latest_tags.get(pkg_name)
                if current is None or parsed_ver > current[0]:
                    latest_tags[pkg_name] = (parsed_ver, tag)

    if not latest_tags:
        shallow_proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
            check=False,
        )
        if shallow_proc.returncode == 0 and shallow_proc.stdout.strip() == "true":
            print(
                "Warning: Shallow git checkout has no '<package>-v*' tags; skipping tag parity check."
            )
            return None

    tag_names: dict[str, str] = {}
    published_extras: dict[str, set[str]] = {}
    for pkg_name, (_, tag_name) in latest_tags.items():
        manifest_rel = workspace_manifests[pkg_name]
        show_proc = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{tag_name}:{manifest_rel}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if show_proc.returncode != 0:
            continue
        tag_data = tomllib.loads(show_proc.stdout)
        opt_deps = tag_data.get("project", {}).get("optional-dependencies", {})
        extras_set = (
            {str(k).strip().lower() for k in opt_deps} if isinstance(opt_deps, dict) else set()
        )
        tag_names[pkg_name] = tag_name
        published_extras[pkg_name] = extras_set

    return tag_names, published_extras, set(workspace_manifests.keys())


def _extract_pip_install_specs(cmd_text: str) -> list[tuple[str, list[str]]]:
    """Parse pip install command text and return (normalized_pkg_name, [extras]) specs."""
    try:
        tokens = shlex.split(cmd_text, comments=True)
    except ValueError:
        return []

    specs: list[tuple[str, list[str]]] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        is_pip_install = (
            (tok in ("pip", "pip3") or tok.endswith("/pip"))
            and i + 1 < n
            and tokens[i + 1] == "install"
        )
        if not is_pip_install:
            i += 1
            continue
        i += 2
        skip_next = False
        while i < n:
            arg = tokens[i]
            i += 1
            if skip_next:
                skip_next = False
                continue
            if arg in ("&&", "||", ";", "|"):
                break
            if arg in (
                "-r",
                "--requirement",
                "-c",
                "--constraint",
                "-f",
                "--find-links",
                "-i",
                "--index-url",
                "--extra-index-url",
                "--target",
                "-t",
                "--prefix",
            ):
                skip_next = True
                continue
            if arg in ("-e", "--editable"):
                # Editable path target follows; skip local path specs while still flagging bare package names if misused
                if i < n and (
                    tokens[i].startswith((".", "/", "~")) or "/" in tokens[i] or "\\" in tokens[i]
                ):
                    i += 1
                continue
            if arg.startswith("-"):
                continue
            if arg.startswith((".", "/", "~")) or "/" in arg or "\\" in arg:
                continue
            req_match = _PIP_REQ_TOKEN_RE.match(arg)
            if not req_match:
                continue
            pkg_norm = _normalize_dist_name(req_match.group(1))
            raw_extras = req_match.group(2) or ""
            extras = [item.strip().lower() for item in raw_extras.split(",") if item.strip()]
            specs.append((pkg_norm, extras))
    return specs


def check_pypi_install_tags(repo_root: Path | None = None) -> bool:
    """Verify PyPI install blocks in READMEs advertise only packages and extras in latest <package>-v* tags."""
    print("Checking README PyPI install commands against published <package>-v* git tags...")
    root = repo_root or REPO_ROOT
    if not (root / "packages").is_dir():
        print(f"Error: Path {root / 'packages'} not found.")
        return True
    discovered = _discover_published_tag_extras(root)
    if discovered is None:
        return False
    latest_tag_names, published_extras, workspace_pkgs = discovered
    if not workspace_pkgs:
        print(f"Error: No workspace packages found under {root / 'packages'}.")
        return True

    readme_files: list[Path] = []
    root_readme = root / "README.md"
    if root_readme.is_file():
        readme_files.append(root_readme)
    packages_dir = root / "packages"
    if packages_dir.is_dir():
        readme_files.extend(sorted(packages_dir.glob("*/README.md")))

    has_errors = False
    checked_specs = 0

    for readme_path in readme_files:
        lines = readme_path.read_text(encoding="utf-8").splitlines()
        heading_stack: dict[int, str] = {}
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            heading_match = _MD_HEADING_RE.match(line)
            if heading_match:
                level = len(heading_match.group(1))
                for existing_lvl in [lvl for lvl in heading_stack if lvl >= level]:
                    del heading_stack[existing_lvl]
                heading_stack[level] = heading_match.group(2).strip().lower()
                i += 1
                continue

            in_source_section = any(
                any(keyword in heading_text for keyword in _SOURCE_SECTION_KEYWORDS)
                for heading_text in heading_stack.values()
            )

            if re.match(r"^\s*```(?:bash|sh|shell|console|zsh)?\s*$", line):
                fence_start = i + 2
                i += 1
                block_lines: list[tuple[int, str]] = []
                while i < n and not re.match(r"^\s*```\s*$", lines[i]):
                    block_lines.append((fence_start + len(block_lines), lines[i]))
                    i += 1
                i += 1
                if in_source_section:
                    continue

                # Join line continuations inside shell block
                logical_cmds: list[tuple[int, str]] = []
                acc_line = ""
                acc_start = fence_start
                for cmd_lineno, raw_cmd in block_lines:
                    stripped = raw_cmd.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if not acc_line:
                        acc_start = cmd_lineno
                    if stripped.endswith("\\"):
                        acc_line += stripped[:-1] + " "
                    else:
                        acc_line += stripped
                        logical_cmds.append((acc_start, acc_line))
                        acc_line = ""
                if acc_line:
                    logical_cmds.append((acc_start, acc_line))

                for cmd_lineno, cmd_str in logical_cmds:
                    for pkg_norm, extras in _extract_pip_install_specs(cmd_str):
                        if pkg_norm not in workspace_pkgs:
                            continue
                        checked_specs += 1
                        if pkg_norm not in published_extras:
                            print(
                                f"Error: Unpublished workspace package '{pkg_norm}' in PyPI install block "
                                f"at {readme_path}:{cmd_lineno} (no '{pkg_norm}-v*' git tag exists; "
                                "move to a Source Checkout section)."
                            )
                            has_errors = True
                            continue
                        allowed_extras = published_extras[pkg_norm]
                        for extra in extras:
                            if extra not in allowed_extras:
                                print(
                                    f"Error: Unreleased extra '{pkg_norm}[{extra}]' in PyPI install block "
                                    f"at {readme_path}:{cmd_lineno} (latest tag {latest_tag_names[pkg_norm]} "
                                    f"declares extras: {sorted(allowed_extras)})."
                                )
                                has_errors = True
                continue

            if not in_source_section:
                for inline_match in _INLINE_CODE_SPAN_RE.finditer(line):
                    inline_code = inline_match.group(1)
                    if "pip install" not in inline_code:
                        continue
                    for pkg_norm, extras in _extract_pip_install_specs(inline_code):
                        if pkg_norm not in workspace_pkgs:
                            continue
                        checked_specs += 1
                        if pkg_norm not in published_extras:
                            print(
                                f"Error: Unpublished workspace package '{pkg_norm}' in inline PyPI install "
                                f"at {readme_path}:{i + 1} (no '{pkg_norm}-v*' git tag exists)."
                            )
                            has_errors = True
                            continue
                        allowed_extras = published_extras[pkg_norm]
                        for extra in extras:
                            if extra not in allowed_extras:
                                print(
                                    f"Error: Unreleased extra '{pkg_norm}[{extra}]' in inline PyPI install "
                                    f"at {readme_path}:{i + 1} (latest tag {latest_tag_names[pkg_norm]} "
                                    f"declares extras: {sorted(allowed_extras)})."
                                )
                                has_errors = True
            i += 1

    if not has_errors:
        print(
            f"README PyPI install tag check passed ({checked_specs} published package/extra spec(s) verified)."
        )
    return has_errors


def _evaluate_conf_pyodide_wheels(
    conf_path: Path, repo_root: Path, core_expected: str
) -> tuple[list[str] | None, str | None]:
    """Extract and evaluate _resolve_pyodide_wheels() from docs/conf.py without Sphinx side effects."""
    try:
        conf_text = conf_path.read_text(encoding="utf-8")
        tree = ast.parse(conf_text, filename=str(conf_path))
    except Exception as exc:
        return None, f"Failed to parse {conf_path}: {exc}"

    selected_nodes: list[ast.stmt] = []
    has_resolver_fn = False
    has_pyodide_config_call = False

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_names = {t.id for t in targets if isinstance(t, ast.Name)}
            if "_PYODIDE_WHEEL_SPECS" in target_names:
                selected_nodes.append(node)
            if "pyodide_config" in target_names:
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_resolve_pyodide_wheels"
                    ):
                        has_pyodide_config_call = True
        elif isinstance(node, ast.FunctionDef) and node.name == "_resolve_pyodide_wheels":
            selected_nodes.append(node)
            has_resolver_fn = True

    if not has_resolver_fn:
        return None, f"{conf_path} is missing '_resolve_pyodide_wheels()'."
    if not has_pyodide_config_call:
        return None, f"{conf_path} pyodide_config does not call '_resolve_pyodide_wheels()'."

    mod = ast.Module(body=selected_nodes, type_ignores=[])
    ast.fix_missing_locations(mod)
    namespace: dict[str, Any] = {
        "Path": Path,
        "tomllib": tomllib,
        "_DOCS_DIR": str(repo_root / "docs"),
        "_REPO_ROOT": str(repo_root),
        "imednet_version": core_expected,
    }
    try:
        exec(compile(mod, str(conf_path), "exec"), namespace)  # noqa: S102
        resolved = namespace["_resolve_pyodide_wheels"]()
    except Exception as exc:
        return None, f"Error evaluating _resolve_pyodide_wheels() in {conf_path}: {exc}"

    if not isinstance(resolved, list) or not all(isinstance(item, str) for item in resolved):
        return None, f"_resolve_pyodide_wheels() in {conf_path} must return list[str]."
    return resolved, None


def check_release_please_parity(
    repo_root: Path | None = None,
    html_dir: Path | None = None,
    require_html: bool = False,
) -> bool:
    """Verify release-please-config.json extra-files, dynamic Pyodide wheels, and manifest parity."""
    print("Checking Release Please extra-files and manifest version parity...")
    root = repo_root or REPO_ROOT
    config_path = root / "release-please-config.json"
    manifest_path = root / ".release-please-manifest.json"
    core_init_path = root / "packages" / "core" / "src" / "imednet" / "__init__.py"
    thebe_js_path = root / "docs" / "_static" / "thebe_pyodide.js"
    conf_path = root / "docs" / "conf.py"

    for req_path in (config_path, manifest_path, core_init_path, thebe_js_path, conf_path):
        if not req_path.is_file():
            print(f"Error: Required Release Please / fallback version file missing: {req_path}")
            return True

    try:
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Error parsing Release Please JSON configuration: {exc}")
        return True

    packages_cfg = config_data.get("packages")
    if (
        not isinstance(packages_cfg, dict)
        or not isinstance(manifest_data, dict)
        or not manifest_data
    ):
        print("Error: Invalid 'packages' or empty manifest in Release Please configuration.")
        return True

    has_errors = False

    def _normalize_extra_path(pkg_key: str, entry: Any) -> str | None:
        raw_path = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        if raw_path.startswith("/"):
            return raw_path.lstrip("/")
        return f"{pkg_key.rstrip('/')}/{raw_path.lstrip('/')}"

    declared_extra_paths: set[str] = set()
    for cfg_pkg_key, cfg_pkg_entry in sorted(packages_cfg.items()):
        if isinstance(cfg_pkg_entry, dict):
            for ef in cfg_pkg_entry.get("extra-files", []):
                norm = _normalize_extra_path(cfg_pkg_key, ef)
                if norm:
                    declared_extra_paths.add(norm)

    for pkg_key, expected_ver in sorted(manifest_data.items()):
        if pkg_key not in packages_cfg:
            print(
                f"Error: '{pkg_key}' in .release-please-manifest.json is missing from release-please-config.json."
            )
            has_errors = True
            continue

        pyproject_file = root / pkg_key / "pyproject.toml"
        if not pyproject_file.is_file():
            print(f"Error: Missing {pyproject_file} for manifest package '{pkg_key}'.")
            has_errors = True
        else:
            pyproject_ver = (
                tomllib.loads(pyproject_file.read_text(encoding="utf-8"))
                .get("project", {})
                .get("version")
            )
            if pyproject_ver != expected_ver:
                print(
                    f"Error: {pyproject_file} version '{pyproject_ver}' != manifest '{expected_ver}'."
                )
                has_errors = True

    required_extra = "packages/core/src/imednet/__init__.py"
    if required_extra not in declared_extra_paths:
        print(
            f"Error: release-please-config.json is missing extra-files entry for '{required_extra}'."
        )
        has_errors = True

    for extra_path in sorted(declared_extra_paths):
        if extra_path.endswith((".js", ".whl")):
            print(
                f"Error: release-please-config.json must not target JavaScript or wheel asset '{extra_path}'."
            )
            has_errors = True

    core_expected = str(manifest_data.get("packages/core", ""))
    core_init_text = core_init_path.read_text(encoding="utf-8")
    init_ver_match = re.search(
        r'^\s*__version__\s*(?::\s*str\s*)?=\s*["\']([^"\']+)["\'].*x-release-please-version',
        core_init_text,
        re.MULTILINE,
    )
    if not init_ver_match:
        print(
            "Error: packages/core/src/imednet/__init__.py fallback __version__ is missing '# x-release-please-version'."
        )
        has_errors = True
    elif init_ver_match.group(1) != core_expected:
        print(
            f"Error: packages/core/src/imednet/__init__.py fallback __version__ '{init_ver_match.group(1)}' != manifest '{core_expected}'."
        )
        has_errors = True

    thebe_text = thebe_js_path.read_text(encoding="utf-8")
    if "x-release-please-version" in thebe_text:
        print(
            "Error: docs/_static/thebe_pyodide.js must not contain 'x-release-please-version' comments."
        )
        has_errors = True

    hardcoded_whl_match = re.search(
        r"[A-Za-z0-9_.-]+-[0-9]+\.[0-9]+\.[0-9]+[^\s\"'`]*\.whl",
        thebe_text,
    )
    if hardcoded_whl_match:
        print(
            "Error: docs/_static/thebe_pyodide.js contains hardcoded versioned wheel path "
            f"'{hardcoded_whl_match.group(0)}'."
        )
        has_errors = True

    for required_js_token in ("_static/wheels/manifest.json", "docRootUrl", "wheelManifestUrl"):
        if required_js_token not in thebe_text:
            print(
                f"Error: docs/_static/thebe_pyodide.js is missing '{required_js_token}' fallback manifest support."
            )
            has_errors = True

    wheel_manifest_map = (
        ("imednet", "packages/core"),
        ("imednet_workflows", "packages/plugins-workflows"),
        ("imednet_plugins_sinks", "packages/plugins-sinks"),
    )
    forbidden_wheel_substrings = (
        "imednet_workspace",
        "imednet-workspace",
        "apache_airflow_providers_imednet",
        "apache-airflow-providers-imednet",
        "imednet_tui",
        "imednet-tui",
    )
    expected_browser_wheels: list[str] = []
    for wheel_prefix, manifest_key in wheel_manifest_map:
        if manifest_key not in manifest_data:
            print(f"Error: '{manifest_key}' missing from .release-please-manifest.json.")
            has_errors = True
            continue
        expected_browser_wheels.append(
            f"_static/wheels/{wheel_prefix}-{manifest_data[manifest_key]}-py3-none-any.whl"
        )

    resolved_conf_wheels, conf_err = _evaluate_conf_pyodide_wheels(conf_path, root, core_expected)
    if conf_err is not None:
        print(f"Error: {conf_err}")
        has_errors = True
    elif resolved_conf_wheels is not None:
        for wheel_entry in resolved_conf_wheels:
            if any(forbidden in wheel_entry for forbidden in forbidden_wheel_substrings):
                print(
                    f"Error: docs/conf.py _resolve_pyodide_wheels() includes non-browser wheel '{wheel_entry}'."
                )
                has_errors = True
        if expected_browser_wheels and resolved_conf_wheels != expected_browser_wheels:
            print(
                f"Error: docs/conf.py _resolve_pyodide_wheels() resolved {resolved_conf_wheels} != expected {expected_browser_wheels}."
            )
            has_errors = True

    if require_html:
        target_html_dir = html_dir if html_dir is not None else (root / "docs" / "_build" / "html")
        wheel_manifest_file = target_html_dir / "_static" / "wheels" / "manifest.json"
        if not wheel_manifest_file.is_file():
            print(f"Error: Missing built Pyodide wheel manifest: {wheel_manifest_file}")
            has_errors = True
        else:
            try:
                wheel_manifest_payload = json.loads(wheel_manifest_file.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"Error parsing {wheel_manifest_file}: {exc}")
                has_errors = True
                wheel_manifest_payload = None

            if not isinstance(wheel_manifest_payload, dict) or not isinstance(
                wheel_manifest_payload.get("wheels"), list
            ):
                print(f"Error: {wheel_manifest_file} must be a JSON object with a 'wheels' list.")
                has_errors = True
            else:
                manifest_wheels = [str(w) for w in wheel_manifest_payload["wheels"]]
                for wheel_entry in manifest_wheels:
                    if any(forbidden in wheel_entry for forbidden in forbidden_wheel_substrings):
                        print(
                            f"Error: {wheel_manifest_file} includes non-browser wheel '{wheel_entry}'."
                        )
                        has_errors = True
                    wheel_disk_path = target_html_dir / wheel_entry
                    if not wheel_disk_path.is_file():
                        print(
                            f"Error: Staged wheel listed in {wheel_manifest_file} is missing: {wheel_disk_path}"
                        )
                        has_errors = True
                if expected_browser_wheels and manifest_wheels != expected_browser_wheels:
                    print(
                        f"Error: {wheel_manifest_file} wheels {manifest_wheels} != expected {expected_browser_wheels}."
                    )
                    has_errors = True

    if not has_errors:
        print("Release Please extra-files and manifest version parity verified.")
    return has_errors


def _parse_rst_toctree_entries(rst_text: str) -> list[tuple[str | None, list[str]]]:
    """Parse '.. toctree::' blocks from RST text into (caption, entries) pairs."""
    lines = rst_text.splitlines()
    n = len(lines)
    results: list[tuple[str | None, list[str]]] = []
    i = 0
    while i < n:
        match = re.match(r"^(\s*)\.\.\s+toctree::\s*$", lines[i])
        if not match:
            i += 1
            continue
        base_indent = len(match.group(1))
        caption: str | None = None
        entries: list[str] = []
        i += 1
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent:
                break
            opt_match = re.match(r"^:([a-zA-Z0-9_-]+):\s*(.*)$", stripped)
            if opt_match:
                if opt_match.group(1) == "caption":
                    caption = opt_match.group(2).strip()
                i += 1
                continue
            entry_target = stripped
            angle_match = re.search(r"<([^>]+)>\s*$", stripped)
            if angle_match:
                entry_target = angle_match.group(1).strip()
            entries.append(entry_target)
            i += 1
        results.append((caption, entries))
    return results


def check_examples_index_parity(repo_root: Path | None = None) -> bool:
    """Verify every examples/**/*.py script has an .rst wrapper indexed in docs/tutorials/examples/index.rst."""
    print("Checking examples/ parity with docs/tutorials/examples/index.rst...")
    root = repo_root or REPO_ROOT
    examples_dir = root / "examples"
    wrappers_dir = root / "docs" / "tutorials" / "examples"
    examples_index = wrappers_dir / "index.rst"

    if not examples_dir.is_dir():
        print(f"Error: Examples directory {examples_dir} not found.")
        return True
    if not examples_index.is_file():
        print(f"Error: Examples index {examples_index} not found.")
        return True

    example_scripts = {
        f"examples/{p.relative_to(examples_dir).as_posix()}"
        for p in sorted(examples_dir.rglob("*.py"))
        if p.name != "__init__.py"
    }
    if not example_scripts:
        print(f"Error: No Python example scripts found under {examples_dir}.")
        return True

    has_errors = False
    toctrees = _parse_rst_toctree_entries(examples_index.read_text(encoding="utf-8"))
    indexed_wrappers: list[str] = []
    for _caption, entries in toctrees:
        indexed_wrappers.extend(entries)

    wrapped_scripts: set[str] = set()
    literalinclude_re = re.compile(r"^\s*\.\.\s+literalinclude::\s+(\S+)\s*$", re.MULTILINE)
    for entry in indexed_wrappers:
        wrapper_path = wrappers_dir / (entry if entry.endswith(".rst") else f"{entry}.rst")
        if not wrapper_path.is_file():
            print(f"Error: Indexed example wrapper {wrapper_path} does not exist.")
            has_errors = True
            continue
        wrapper_text = wrapper_path.read_text(encoding="utf-8")
        matches = literalinclude_re.findall(wrapper_text)
        if not matches:
            print(f"Error: Example wrapper {wrapper_path} is missing '.. literalinclude::'.")
            has_errors = True
            continue
        for raw_target in matches:
            if raw_target.startswith("/"):
                resolved = (root / "docs" / raw_target.lstrip("/")).resolve()
            else:
                resolved = (wrapper_path.parent / raw_target).resolve()
            if not resolved.is_file():
                print(
                    f"Error: Broken literalinclude '{raw_target}' in {wrapper_path} (resolved to {resolved})."
                )
                has_errors = True
                continue
            try:
                rel_script = resolved.relative_to(root.resolve()).as_posix()
                wrapped_scripts.add(rel_script)
            except ValueError:
                print(f"Error: literalinclude target {resolved} lies outside {root}.")
                has_errors = True

    missing_scripts = sorted(example_scripts - wrapped_scripts)
    for missing in missing_scripts:
        print(
            f"Error: Example script '{missing}' has no wrapper indexed in docs/tutorials/examples/index.rst."
        )
        has_errors = True

    if not has_errors:
        print(
            f"Examples index parity passed ({len(example_scripts)} script(s) indexed across {len(indexed_wrappers)} wrapper(s))."
        )
    return has_errors


_INTERNAL_MAINTAINER_DOCS: frozenset[str] = frozenset(
    {
        "explanation/refactoring_patterns",
        "reference/agent_rules",
        "reference/refactoring_roadmaps",
        "reference/test_skip_conditions",
        "reference/live_test_charter",
        "reference/live_test_plan",
        "reference/live_tests",
        "reference/contributing",
        "reference/issue_management",
        "reference/project_standards",
        "reference/triage_playbook",
        "reference/verification_reports",
        "reference/security_exceptions",
    }
)


def check_diataxis_sidebar_isolation(repo_root: Path | None = None) -> bool:
    """Verify internal maintainer/refactoring docs are isolated from user sidebars and deduplicated."""
    print("Checking Diátaxis sidebar isolation and contributor toctree deduplication...")
    root = repo_root or REPO_ROOT
    index_rst = root / "docs" / "index.rst"
    contributing_rst = root / "docs" / "reference" / "contributing.rst"

    if not index_rst.is_file() or not contributing_rst.is_file():
        print(f"Error: Required sidebar docs missing ({index_rst} or {contributing_rst}).")
        return True

    has_errors = False
    toctrees = _parse_rst_toctree_entries(index_rst.read_text(encoding="utf-8"))
    by_caption: dict[str, set[str]] = {}
    for caption, entries in toctrees:
        key = (caption or "").strip()
        by_caption.setdefault(key, set()).update(entries)

    for user_caption in ("Tutorials:", "How-To Guides:", "Explanation:", "Reference:"):
        leaked = sorted(by_caption.get(user_caption, set()) & _INTERNAL_MAINTAINER_DOCS)
        for doc in leaked:
            print(
                f"Error: Internal maintainer/refactoring doc '{doc}' leaked into user '{user_caption}' sidebar."
            )
            has_errors = True

    if "reference/changelogs/index" not in by_caption.get("Reference:", set()):
        print("Error: 'reference/changelogs/index' is missing from user 'Reference:' toctree.")
        has_errors = True

    maintainer_entries = by_caption.get("Maintainer & Contributor:", set())
    missing_maintainer = sorted(_INTERNAL_MAINTAINER_DOCS - maintainer_entries)
    for doc in missing_maintainer:
        print(
            f"Error: Internal maintainer doc '{doc}' is missing from 'Maintainer & Contributor:' toctree."
        )
        has_errors = True

    contrib_toctrees = _parse_rst_toctree_entries(contributing_rst.read_text(encoding="utf-8"))
    if contrib_toctrees:
        print(
            "Error: docs/reference/contributing.rst still contains a duplicate nested '.. toctree::'."
        )
        has_errors = True

    if not has_errors:
        print("Diátaxis user and maintainer sidebars are cleanly separated.")
    return has_errors


def check_adr_records(repo_root: Path | None = None) -> bool:
    """Validate that all ADR records in docs/adr follow naming patterns and have valid titles."""
    print("Checking architecture decision records (ADRs)...")
    root = repo_root or REPO_ROOT
    adr_dir = root / "docs" / "adr"
    if not adr_dir.is_dir():
        print(f"Error: ADR directory {adr_dir} not found.")
        return True

    adr_name_pattern = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")
    has_errors = False
    record_count = 0

    for item in sorted(adr_dir.iterdir()):
        # Ignore hidden files like .gitkeep and generated index files like index.rst
        if item.name.startswith(".") or item.name == "index.rst":
            continue

        if not item.is_file():
            print(f"Error: Unexpected directory in docs/adr: {item.name}")
            has_errors = True
            continue

        if not adr_name_pattern.match(item.name):
            print(
                f"Error: ADR file {item.name} does not match the numeric naming pattern (e.g. '0001-slug-title.md')."
            )
            has_errors = True
            continue

        try:
            content = item.read_text(encoding="utf-8")
        except Exception as e:
            print(f"Error reading {item.name}: {e}")
            has_errors = True
            continue

        non_empty_lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not non_empty_lines:
            print(f"Error: ADR record {item.name} is empty.")
            has_errors = True
            continue

        first_line = non_empty_lines[0]
        if not first_line.startswith("# ") or not first_line[2:].strip():
            print(
                f"Error: ADR record {item.name} must start with a valid level-1 heading ('# <Title>')."
            )
            has_errors = True
            continue

        record_count += 1

    if record_count == 0:
        print(f"Error: No valid ADR records found in {adr_dir}.")
        has_errors = True

    if not has_errors:
        print(f"All {record_count} ADR records passed validation.")
    return has_errors


def check_sphinx_githubpages_extension() -> bool:
    """Validate that sphinx.ext.githubpages is registered in docs/conf.py."""
    print("Checking for sphinx.ext.githubpages in docs/conf.py...")
    conf_py = Path(__file__).resolve().parents[1] / "docs" / "conf.py"
    if not conf_py.exists():
        print(f"Error: {conf_py} not found.")
        return True

    try:
        content = conf_py.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(conf_py))
    except Exception as e:
        print(f"Error parsing {conf_py}: {e}")
        return True

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "extensions"
                    and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))
                ):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and elt.value == "sphinx.ext.githubpages":
                            print("sphinx.ext.githubpages extension is correctly registered.")
                            return False

    print("Error: sphinx.ext.githubpages is missing from extensions in docs/conf.py.")
    return True


def check_sphinx_html_baseurl() -> bool:
    """Validate that html_baseurl is configured with the expected canonical URL in docs/conf.py."""
    print("Checking for html_baseurl in docs/conf.py...")
    conf_py = Path(__file__).resolve().parents[1] / "docs" / "conf.py"
    if not conf_py.exists():
        print(f"Error: {conf_py} not found.")
        return True

    try:
        content = conf_py.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(conf_py))
    except Exception as e:
        print(f"Error parsing {conf_py}: {e}")
        return True

    expected_baseurl = "https://fderuiter.github.io/imednet-toolkit/"
    for node in ast.walk(tree):
        val = None
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "html_baseurl"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    val = node.value.value
                    break
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "html_baseurl"
            and node.value
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            val = node.value.value

        if val is not None:
            if not val.startswith("https://"):
                print(f"Error: html_baseurl must start with 'https://', got '{val}'.")
                return True
            if not val.endswith("/"):
                print(f"Error: html_baseurl must end with a trailing slash '/', got '{val}'.")
                return True
            if val != expected_baseurl:
                print(f"Error: html_baseurl is '{val}', expected '{expected_baseurl}'.")
                return True
            print("html_baseurl is correctly configured.")
            return False

    print("Error: html_baseurl is missing from docs/conf.py.")
    return True


class _DlFrame:
    """Mutable stack frame tracking a <dl> block while parsing Sphinx API HTML."""

    __slots__ = (
        "child_dl_count",
        "chunks",
        "classes",
        "dd_depth",
        "dd_non_p_chunks",
        "dd_p_chunks",
        "dd_paragraphs",
        "dt_ids",
        "dt_texts",
        "in_dd_p",
    )

    def __init__(self, classes: set[str]) -> None:
        self.classes = classes
        self.dt_ids: list[str] = []
        self.dt_texts: list[str] = []
        self.chunks: list[str] = []
        self.child_dl_count = 0
        self.dd_depth = 0
        self.in_dd_p = False
        self.dd_p_chunks: list[str] = []
        self.dd_paragraphs: list[str] = []
        self.dd_non_p_chunks: list[str] = []

    @property
    def has_non_bases_dd_content(self) -> bool:
        if self.child_dl_count > 0:
            return True
        if any(p and not p.startswith("Bases:") for p in self.dd_paragraphs):
            return True
        return bool(" ".join("".join(self.dd_non_p_chunks).split()))


class _ApiHtmlExtractor(HTMLParser):
    """Lightweight stdlib HTML parser collecting IDs, dl blocks, multi-dt headers, and links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.dt_text: dict[str, str] = {}
        self.dt_list_by_id: dict[str, list[str]] = {}
        self.dl_text_by_dt_id: dict[str, str] = {}
        self.dl_classes_by_id: dict[str, set[str]] = {}
        self.empty_class_ids: list[str] = []
        self.backlinks: list[str] = []
        self.backlink_labels: list[tuple[str, str | None]] = []
        self.headerlinks: list[tuple[str, str | None]] = []
        self.source_links: list[tuple[str, str | None, bool]] = []
        self.internal_hrefs: dict[str, list[str]] = {}
        self._dl_stack: list[_DlFrame] = []
        self._in_dt = False
        self._cur_dt_id: str | None = None
        self._cur_dt_chunks: list[str] = []
        self._cur_a_href: str | None = None
        self._cur_a_aria_label: str | None = None
        self._cur_a_classes: set[str] = set()
        self._cur_a_has_viewcode_span = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        elem_id = attr_map.get("id")
        if elem_id:
            self.ids.add(elem_id)

        if tag == "dl":
            if self._dl_stack:
                self._dl_stack[-1].child_dl_count += 1
            dl_classes = set((attr_map.get("class") or "").split())
            self._dl_stack.append(_DlFrame(dl_classes))
        elif tag == "dt":
            self._in_dt = True
            self._cur_dt_id = elem_id
            self._cur_dt_chunks = []
            if elem_id and self._dl_stack:
                self._dl_stack[-1].dt_ids.append(elem_id)
        elif tag == "dd":
            if self._dl_stack:
                self._dl_stack[-1].dd_depth += 1
        elif tag == "p":
            if self._dl_stack and self._dl_stack[-1].dd_depth > 0 and not self._in_dt:
                self._dl_stack[-1].in_dd_p = True
                self._dl_stack[-1].dd_p_chunks = []
        elif tag == "a":
            classes = (attr_map.get("class") or "").split()
            class_set = set(classes)
            href = attr_map.get("href")
            aria_label = attr_map.get("aria-label")
            self._cur_a_href = href
            self._cur_a_aria_label = aria_label
            self._cur_a_classes = class_set
            self._cur_a_has_viewcode_span = False
            if "headerlink" in class_set:
                self.headerlinks.append((href or "", aria_label))
            if "viewcode-back" in class_set and href:
                self.backlinks.append(href)
                self.backlink_labels.append((href, aria_label))
            if "reference" in class_set and "internal" in class_set and href and self._dl_stack:
                for dt_id in self._dl_stack[-1].dt_ids:
                    self.internal_hrefs.setdefault(dt_id, []).append(href)
        elif tag == "span" and self._cur_a_href is not None:
            span_classes = set((attr_map.get("class") or "").split())
            if "viewcode-link" in span_classes:
                self._cur_a_has_viewcode_span = True

    def handle_data(self, data: str) -> None:
        if self._in_dt:
            self._cur_dt_chunks.append(data)
        for frame in self._dl_stack:
            frame.chunks.append(data)
        if self._dl_stack and not self._in_dt:
            top = self._dl_stack[-1]
            if top.dd_depth > 0:
                if top.in_dd_p:
                    top.dd_p_chunks.append(data)
                else:
                    top.dd_non_p_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._cur_a_href is not None:
            if "viewcode-source-link" in self._cur_a_classes or self._cur_a_has_viewcode_span:
                self.source_links.append(
                    (
                        self._cur_a_href,
                        self._cur_a_aria_label,
                        "viewcode-source-link" in self._cur_a_classes,
                    )
                )
            self._cur_a_href = None
            self._cur_a_aria_label = None
            self._cur_a_classes = set()
            self._cur_a_has_viewcode_span = False
        elif tag == "dt" and self._in_dt:
            dt_norm = " ".join("".join(self._cur_dt_chunks).split())
            if self._cur_dt_id is not None:
                self.dt_text[self._cur_dt_id] = dt_norm
            if self._dl_stack and dt_norm:
                self._dl_stack[-1].dt_texts.append(dt_norm)
            self._in_dt = False
            self._cur_dt_id = None
            self._cur_dt_chunks = []
        elif tag == "p":
            if self._dl_stack and self._dl_stack[-1].in_dd_p:
                top = self._dl_stack[-1]
                p_norm = " ".join("".join(top.dd_p_chunks).split())
                if p_norm:
                    top.dd_paragraphs.append(p_norm)
                top.in_dd_p = False
                top.dd_p_chunks = []
        elif tag == "dd":
            if self._dl_stack and self._dl_stack[-1].dd_depth > 0:
                self._dl_stack[-1].dd_depth -= 1
        elif tag == "dl" and self._dl_stack:
            frame = self._dl_stack.pop()
            text = " ".join("".join(frame.chunks).split())
            is_py_class = "py" in frame.classes and "class" in frame.classes
            for dt_id in frame.dt_ids:
                self.dl_text_by_dt_id.setdefault(dt_id, text)
                self.dt_list_by_id.setdefault(dt_id, list(frame.dt_texts))
                self.dl_classes_by_id.setdefault(dt_id, set(frame.classes))
                if is_py_class and not frame.has_non_bases_dd_content:
                    self.empty_class_ids.append(dt_id)


def _parse_html_file(path: Path) -> _ApiHtmlExtractor:
    parser = _ApiHtmlExtractor()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def check_no_duplicate_suppression(conf_py_path: Path | None = None) -> bool:
    """Verify _SuppressDuplicateDescriptions and duplicate warning filters are absent from docs/conf.py."""
    print("Checking docs/conf.py for absence of duplicate warning suppression...")
    if conf_py_path is None:
        conf_py_path = Path(__file__).resolve().parents[1] / "docs" / "conf.py"
    if not conf_py_path.exists():
        print(f"Error: {conf_py_path} not found.")
        return True

    content = conf_py_path.read_text(encoding="utf-8")
    has_errors = False
    if "_SuppressDuplicateDescriptions" in content:
        print("Error: _SuppressDuplicateDescriptions is still present in docs/conf.py.")
        has_errors = True
    if "duplicate object description" in content:
        print("Error: 'duplicate object description' warning suppression found in docs/conf.py.")
        has_errors = True
    if not has_errors:
        print("No duplicate warning suppression found in docs/conf.py.")
    return has_errors


def _verify_sdk_html(sdk_page: Path) -> bool:
    """Verify ImednetSDK and AsyncImednetSDK convenience methods and study_context in imednet.html."""
    from imednet.endpoints.registry import ENDPOINT_REGISTRY

    has_errors = False
    sdk_parsed = _parse_html_file(sdk_page)
    list_resources = sorted(k for k, cls in ENDPOINT_REGISTRY.items() if hasattr(cls, "list"))

    for sdk_qname, is_async in (
        ("imednet.sdk.ImednetSDK", False),
        ("imednet.sdk.AsyncImednetSDK", True),
    ):
        if sdk_qname not in sdk_parsed.ids:
            print(f"Error: SDK class '{sdk_qname}' missing from imednet.html.")
            has_errors = True
            continue

        sc_id = f"{sdk_qname}.study_context"
        if sc_id not in sdk_parsed.ids:
            print(f"Error: Inherited method '{sc_id}' missing from imednet.html.")
            has_errors = True

        for resource in list_resources:
            method_name = f"async_get_{resource}" if is_async else f"get_{resource}"
            mid = f"{sdk_qname}.{method_name}"
            if mid not in sdk_parsed.ids:
                print(f"Error: Convenience method '{mid}' missing from imednet.html.")
                has_errors = True
                continue
            dt_str = sdk_parsed.dt_text.get(mid, "")
            dl_str = sdk_parsed.dl_text_by_dt_id.get(mid, "")
            if is_async and not dt_str.startswith("async "):
                print(f"Error: Async convenience method '{mid}' missing 'async' prefix.")
                has_errors = True
            if not is_async and dt_str.startswith("async "):
                print(f"Error: Sync convenience method '{mid}' unexpectedly marked 'async'.")
                has_errors = True
            if "sdk_self" in dl_str:
                print(f"Error: Convenience method '{mid}' leaks internal 'sdk_self' parameter.")
                has_errors = True

    return has_errors


def _verify_pagination_html(pagination_page: Path) -> bool:
    """Verify Paginator, JsonListPaginator, AsyncPaginator, and AsyncJsonListPaginator HTML."""
    has_errors = False
    pag_parsed = _parse_html_file(pagination_page)
    expected_paginators = (
        ("Paginator", False),
        ("JsonListPaginator", False),
        ("AsyncPaginator", True),
        ("AsyncJsonListPaginator", True),
    )
    for cls_name, is_async in expected_paginators:
        qname = f"imednet.pagination.{cls_name}"
        if qname not in pag_parsed.ids:
            print(f"Error: Paginator '{qname}' missing from imednet.pagination.html.")
            has_errors = True
            continue

        for attr_name in ("__init__", "cursor", "page_size"):
            aid = f"{qname}.{attr_name}"
            if aid not in pag_parsed.ids:
                print(f"Error: Paginator member '{aid}' missing from imednet.pagination.html.")
                has_errors = True

        iter_name = "__aiter__" if is_async else "__iter__"
        iter_id = f"{qname}.{iter_name}"
        if iter_id not in pag_parsed.ids:
            print(f"Error: Paginator iterator '{iter_id}' missing from imednet.pagination.html.")
            has_errors = True
        else:
            dt_str = pag_parsed.dt_text.get(iter_id, "")
            dl_str = pag_parsed.dl_text_by_dt_id.get(iter_id, "")
            if is_async and not dt_str.startswith("async "):
                print(f"Error: Async paginator '{iter_id}' missing 'async' coroutine prefix.")
                has_errors = True
            expected_ret = "AsyncIterator" if is_async else "Iterator"
            if expected_ret not in dl_str:
                print(f"Error: Paginator '{iter_id}' missing '{expected_ret}' return type.")
                has_errors = True

    return has_errors


def _verify_core_html(core_page: Path) -> bool:
    """Verify curated imednet.core exports, EDC endpoint bases, and clean inheritance."""
    import imednet.core as core_pkg

    has_errors = False
    core_text = core_page.read_text(encoding="utf-8")
    for forbidden_token in (
        "ListOperation",
        "FilterGetOperation",
        "SyncRequestExecutor",
        "AsyncRequestExecutor",
    ):
        if forbidden_token in core_text:
            print(f"Error: Internal type '{forbidden_token}' leaked into imednet.core.html.")
            has_errors = True

    core_parsed = _parse_html_file(core_page)
    expected_exports = {
        "HTTPClientBase",
        "BaseClient",
        "Client",
        "AsyncClient",
        "RetryConfig",
        "RetryPolicy",
        "RetryState",
        "DefaultRetryPolicy",
        "BasePaginator",
        "EdcSyncListEndpoint",
        "EdcAsyncListEndpoint",
        "EdcSyncGetEndpoint",
        "EdcAsyncGetEndpoint",
        "EdcSyncListGetEndpoint",
        "EdcAsyncListGetEndpoint",
    }
    actual_exports = set(getattr(core_pkg, "__all__", ()))
    if not expected_exports.issubset(actual_exports):
        missing = sorted(expected_exports - actual_exports)
        print(f"Error: imednet.core.__all__ is missing expected exports: {missing}.")
        has_errors = True

    for sym in sorted(expected_exports):
        qname = f"imednet.core.{sym}"
        if qname not in core_parsed.ids:
            print(f"Error: Export '{qname}' missing from imednet.core.html.")
            has_errors = True

    edc_checks = (
        ("EdcSyncListEndpoint", False, True, False),
        ("EdcAsyncListEndpoint", True, True, False),
        ("EdcSyncGetEndpoint", False, False, True),
        ("EdcAsyncGetEndpoint", True, False, True),
        ("EdcSyncListGetEndpoint", False, True, True),
        ("EdcAsyncListGetEndpoint", True, True, True),
    )
    for cls_name, is_async, has_list, has_get in edc_checks:
        qname = f"imednet.core.{cls_name}"
        if has_list:
            for mname in ("list", "to_dataframe"):
                mid = f"{qname}.{mname}"
                if mid not in core_parsed.ids:
                    print(f"Error: Core endpoint method '{mid}' missing from imednet.core.html.")
                    has_errors = True
                elif mname == "list":
                    expected_iter = "AsyncIterator" if is_async else "Iterator"
                    if expected_iter not in core_parsed.dl_text_by_dt_id.get(mid, ""):
                        print(f"Error: '{mid}' missing '{expected_iter}' return type.")
                        has_errors = True
                elif is_async and not core_parsed.dt_text.get(mid, "").startswith("async "):
                    print(f"Error: Async method '{mid}' missing 'async' prefix.")
                    has_errors = True
        if has_get:
            mid = f"{qname}.get"
            if mid not in core_parsed.ids:
                print(f"Error: Core endpoint method '{mid}' missing from imednet.core.html.")
                has_errors = True
            elif is_async and not core_parsed.dt_text.get(mid, "").startswith("async "):
                print(f"Error: Async method '{mid}' missing 'async' prefix.")
                has_errors = True

    return has_errors


_FORBIDDEN_API_HTML_TOKENS = (
    "pydantic_core.ValidationError",
    "UnionType",
    "<factory>",
    "&lt;factory&gt;",
)
_TILDE_TYPEVAR_RE = re.compile(r"~[PT]\b")
_EXPECTED_PROTOCOL_ALIASES = {
    "imednet.plugins.RegisterSubjectsWorkflowProtocol": (
        "imednet.plugins.SyncRegisterSubjectsWorkflowProtocol"
    ),
    "imednet.plugins.WorkflowsNamespaceProtocol": (
        "imednet.plugins.SyncWorkflowsNamespaceProtocol"
    ),
    "imednet.plugins.SyncSinksNamespaceProtocol": "imednet.plugins.SinksNamespaceProtocol",
}
_EXPECTED_OVERLOAD_TARGETS = (
    "imednet_sinks.plugin.SinksNamespace.export_to_mongodb",
    "imednet_sinks.plugin.SinksNamespace.export_to_neo4j",
    "imednet_sinks.plugin.SinksNamespace.export_to_snowflake",
    "imednet.plugins.BoundExportToMongoDbProtocol.__call__",
    "imednet.plugins.BoundExportToNeo4jProtocol.__call__",
    "imednet.plugins.BoundExportToSnowflakeProtocol.__call__",
    "imednet.validation.cache.validate_record_data",
    "imednet.validation.cache.validate_record_entry",
    "imednet.validation.cache.SchemaValidator.validate_batch",
    "imednet.validation.cache.AsyncSchemaValidator.validate_batch",
)
_EXPECTED_FACTORY_DATACLASSES = {
    "imednet.core.RetryConfig": "DefaultRetryPolicy()",
    "imednet.integrations.sink_base.SinkConfig": "dict()",
    "imednet.integrations.export.TabularSinkConfig": "dict()",
    "imednet_sinks.document.MongoDbSinkConfig": "dict()",
    "imednet_sinks.graph.Neo4jSinkConfig": "dict()",
    "imednet_sinks.warehouse.SnowflakeSinkConfig": "dict()",
}


def _ensure_workspace_src_on_path(repo_root: Path) -> None:
    """Ensure all workspace package src/ directories are importable."""
    if os.environ.get("IMEDNET_TEST_WHEELS") == "1" and repo_root.resolve() == REPO_ROOT.resolve():
        return
    for pkg_sub in (
        "packages/core/src",
        "packages/plugins-workflows/src",
        "packages/plugins-sinks/src",
        "packages/plugins-tui/src",
        "packages/providers-airflow/src",
    ):
        src_dir = (repo_root / pkg_sub).resolve()
        if src_dir.is_dir() and str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))


def _is_typeddict_cls(cls: type) -> bool:
    """Return True for both typing.TypedDict and typing_extensions.TypedDict classes."""
    if typing.is_typeddict(cls):
        return True
    return (
        isinstance(cls, type)
        and issubclass(cls, dict)
        and isinstance(getattr(cls, "__required_keys__", None), frozenset)
        and isinstance(getattr(cls, "__optional_keys__", None), frozenset)
    )


def _is_protocol_cls(cls: Any) -> bool:
    """Return True if cls is a typing.Protocol subclass other than Protocol itself."""
    return (
        isinstance(cls, type)
        and bool(getattr(cls, "_is_protocol", False))
        and cls.__name__ != "Protocol"
    )


@functools.cache
def _parse_source_ast(file_path: str) -> ast.Module | None:
    """Parse a Python source file once for AST class attribute inspection."""
    try:
        src = Path(file_path).read_text(encoding="utf-8")
        return ast.parse(src, filename=file_path)
    except (OSError, SyntaxError):
        return None


def _collect_class_ann_names(stmts: Sequence[ast.stmt], out: list[str]) -> None:
    """Collect bare AnnAssign attribute names from class body statements (including TYPE_CHECKING)."""
    for stmt in stmts:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out.append(stmt.target.id)
        elif isinstance(stmt, ast.If):
            _collect_class_ann_names(stmt.body, out)
            _collect_class_ann_names(stmt.orelse, out)


def _get_protocol_bare_attr_names(cls: type) -> list[str]:
    """Return public bare attribute annotations across a Protocol's MRO (excluding methods/properties)."""
    collected: list[str] = []
    for ancestor in reversed(cls.__mro__):
        if not _is_protocol_cls(ancestor):
            continue
        ancestor_names: list[str] = list((ancestor.__dict__.get("__annotations__") or {}).keys())
        src_file = None
        with contextlib.suppress(TypeError, OSError):
            src_file = inspect.getsourcefile(ancestor)
        if src_file:
            tree = _parse_source_ast(src_file)
            if tree is not None:
                for node in tree.body:
                    if isinstance(node, ast.ClassDef) and node.name == ancestor.__name__:
                        _collect_class_ann_names(node.body, ancestor_names)
                        break
        for name in ancestor_names:
            if name.startswith("_") or name in ancestor.__dict__:
                continue
            if any(name in sub.__dict__ for sub in cls.__mro__ if sub is not object):
                continue
            if name not in collected:
                collected.append(name)
    return collected


def _resolve_dotted_symbol(dotted: str) -> tuple[str, Any] | None:
    """Resolve a dotted HTML id into (module_name, runtime_object) if importable."""
    parts = dotted.split(".")
    for idx in range(len(parts) - 1, 0, -1):
        mod_candidate = ".".join(parts[:idx])
        try:
            mod = importlib.import_module(mod_candidate)
        except Exception:
            continue
        cur: Any = mod
        found = True
        for attr in parts[idx:]:
            if inspect.ismodule(cur):
                cur = getattr(cur, attr, None)
            else:
                cur = inspect.getattr_static(cur, attr, None)
            if cur is None:
                found = False
                break
        if found:
            return mod_candidate, cur
    return None


def check_no_autodoc_typehints_dependency(repo_root: Path | None = None) -> bool:
    """Verify sphinx-autodoc-typehints is absent from pyproject.toml and docs/conf.py."""
    root = repo_root or Path(__file__).resolve().parents[1]
    has_errors = False
    pyproject_path = root / "pyproject.toml"
    if pyproject_path.is_file():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        docs_group = data.get("dependency-groups", {}).get("docs", [])
        if any("sphinx-autodoc-typehints" in str(dep) for dep in docs_group):
            print(
                "Error: sphinx-autodoc-typehints must not appear in pyproject.toml [dependency-groups].docs."
            )
            has_errors = True
    conf_path = root / "docs" / "conf.py"
    if conf_path.is_file():
        conf_text = conf_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(conf_text, filename=str(conf_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "sphinx_autodoc_typehints":
                    print(
                        "Error: 'sphinx_autodoc_typehints' must not be configured in docs/conf.py."
                    )
                    has_errors = True
                    break
        except SyntaxError as exc:
            print(f"Error parsing {conf_path}: {exc}")
            has_errors = True
    return has_errors


def _verify_api_reference_parity(api_html_dir: Path, repo_root: Path | None = None) -> bool:
    """Verify rendered API HTML parity across Enums, dataclasses, TypedDicts, Protocols, BaseModels, and @overloads."""
    root = repo_root or Path(__file__).resolve().parents[1]
    _ensure_workspace_src_on_path(root)

    from pydantic import BaseModel, RootModel

    has_errors = check_no_autodoc_typehints_dependency(root)
    html_files = sorted(api_html_dir.glob("*.html"))
    if not html_files:
        print(f"Error: No API HTML files found in {api_html_dir}.")
        return True

    parsed_pages: dict[Path, _ApiHtmlExtractor] = {}
    page_by_id: dict[str, Path] = {}

    for html_file in html_files:
        raw_html = html_file.read_text(encoding="utf-8")
        for bad_token in _FORBIDDEN_API_HTML_TOKENS:
            if bad_token in raw_html:
                print(f"Error: Forbidden token '{bad_token}' found in {html_file.name}.")
                has_errors = True

        parsed = _parse_html_file(html_file)
        parsed_pages[html_file] = parsed

        for empty_cid in parsed.empty_class_ids:
            print(f"Error: Empty class shell '{empty_cid}' found in {html_file.name}.")
            has_errors = True

        for dt_id, dl_text in parsed.dl_text_by_dt_id.items():
            match = _TILDE_TYPEVAR_RE.search(dl_text)
            if match:
                print(
                    f"Error: Unresolved TypeVar/ParamSpec prefix '{match.group(0)}' in '{dt_id}' ({html_file.name})."
                )
                has_errors = True

        for elem_id in parsed.ids:
            page_by_id.setdefault(elem_id, html_file)

    enum_classes = 0
    enum_values = 0
    dataclass_classes = 0
    dataclass_fields = 0
    typeddict_classes = 0
    typeddict_keys = 0
    protocol_sink_attrs = 0
    callable_protocols = 0
    basemodel_classes = 0
    basemodel_fields = 0
    overload_callables = 0
    overload_headers = 0
    seen_qnames: set[str] = set()

    for html_file, parsed in parsed_pages.items():
        for dt_id, dl_classes in sorted(parsed.dl_classes_by_id.items()):
            if "py" not in dl_classes or dt_id in seen_qnames:
                continue

            if "class" in dl_classes or "exception" in dl_classes:
                resolved = _resolve_dotted_symbol(dt_id)
                if resolved is None:
                    continue
                _mod_name, obj = resolved
                if not isinstance(obj, type):
                    continue
                sym_tail = dt_id.rsplit(".", 1)[-1]
                if sym_tail != obj.__name__:
                    continue
                seen_qnames.add(dt_id)

                # 1. Enum classes and literal values
                if issubclass(obj, Enum) and obj is not Enum:
                    enum_classes += 1
                    for mname, member in obj.__members__.items():
                        mid = f"{dt_id}.{mname}"
                        if mid not in parsed.ids:
                            print(f"Error: Enum member '{mid}' missing from {html_file.name}.")
                            has_errors = True
                            continue
                        m_dt = parsed.dt_text.get(mid, "")
                        expected_lit = repr(member.value)
                        if "=" not in m_dt or expected_lit not in m_dt:
                            print(
                                f"Error: Enum member '{mid}' missing literal value {expected_lit} in {html_file.name}."
                            )
                            has_errors = True
                        else:
                            enum_values += 1

                # 2. First-party @dataclass classes, readable factories, and repr=False masking
                if "__dataclass_params__" in obj.__dict__:
                    dataclass_classes += 1
                    cls_dt = parsed.dt_text.get(dt_id, "")
                    init_dt = parsed.dt_text.get(f"{dt_id}.__init__", "")
                    sig_surfaces = [s for s in (cls_dt, init_dt) if "(" in s]
                    for fld in dataclasses.fields(obj):
                        if fld.name.startswith("_"):
                            continue
                        fid = f"{dt_id}.{fld.name}"
                        if fid not in parsed.ids:
                            print(f"Error: Dataclass field '{fid}' missing from {html_file.name}.")
                            has_errors = True
                            continue
                        f_dt = parsed.dt_text.get(fid, "")
                        f_dl = parsed.dl_text_by_dt_id.get(fid, "")
                        if ":" not in f_dt:
                            print(
                                f"Error: Dataclass field '{fid}' missing type annotation in {html_file.name}."
                            )
                            has_errors = True
                        is_req = (
                            fld.default is dataclasses.MISSING
                            and fld.default_factory is dataclasses.MISSING
                        )
                        if is_req:
                            if "Required." not in f_dl:
                                print(
                                    f"Error: Required dataclass field '{fid}' missing 'Required.' marker in {html_file.name}."
                                )
                                has_errors = True
                            else:
                                dataclass_fields += 1
                        elif not fld.repr:
                            if "= ..." not in f_dt:
                                print(
                                    f"Error: repr=False dataclass field '{fid}' must mask default with '= ...' in {html_file.name}."
                                )
                                has_errors = True
                            elif (
                                fld.default is not dataclasses.MISSING
                                and fld.default is not None
                                and f"= {fld.default!r}" in f_dt
                            ):
                                print(
                                    f"Error: repr=False dataclass field '{fid}' leaked default {fld.default!r} in {html_file.name}."
                                )
                                has_errors = True
                            else:
                                dataclass_fields += 1
                            if fld.init:
                                for surf in sig_surfaces:
                                    if (
                                        fld.default is not dataclasses.MISSING
                                        and f"{fld.name}={fld.default!r}" in surf
                                    ):
                                        print(
                                            f"Error: repr=False parameter '{fld.name}' on '{dt_id}' leaked default {fld.default!r} in constructor signature ({html_file.name})."
                                        )
                                        has_errors = True
                                    elif f"{fld.name}=..." not in surf:
                                        print(
                                            f"Error: repr=False parameter '{fld.name}' on '{dt_id}' must render '{fld.name}=...' in constructor signature ({html_file.name})."
                                        )
                                        has_errors = True
                        elif "=" not in f_dt:
                            print(
                                f"Error: Dataclass field '{fid}' missing default value in {html_file.name}."
                            )
                            has_errors = True
                        else:
                            dataclass_fields += 1

                # 3. TypedDict classes and Required/Optional keys
                if _is_typeddict_cls(obj):
                    typeddict_classes += 1
                    req_keys: frozenset[str] = getattr(obj, "__required_keys__", frozenset())
                    opt_keys: frozenset[str] = getattr(obj, "__optional_keys__", frozenset())
                    for kname in sorted(req_keys | opt_keys):
                        kid = f"{dt_id}.{kname}"
                        if kid not in parsed.ids:
                            print(f"Error: TypedDict key '{kid}' missing from {html_file.name}.")
                            has_errors = True
                            continue
                        k_dt = parsed.dt_text.get(kid, "")
                        k_dl = parsed.dl_text_by_dt_id.get(kid, "")
                        expected_marker = "Required." if kname in req_keys else "Optional."
                        if ":" not in k_dt or expected_marker not in k_dl:
                            print(
                                f"Error: TypedDict key '{kid}' missing type or '{expected_marker}' marker in {html_file.name}."
                            )
                            has_errors = True
                        else:
                            typeddict_keys += 1

                # 4. Protocol bare attributes and callable __call__ methods
                if _is_protocol_cls(obj):
                    bare_attrs = _get_protocol_bare_attr_names(obj)
                    for aname in bare_attrs:
                        aid = f"{dt_id}.{aname}"
                        if aid not in parsed.ids:
                            print(
                                f"Error: Protocol attribute '{aid}' missing from {html_file.name}."
                            )
                            has_errors = True
                        elif ":" not in parsed.dt_text.get(aid, ""):
                            print(
                                f"Error: Protocol attribute '{aid}' missing type annotation in {html_file.name}."
                            )
                            has_errors = True
                        elif dt_id in (
                            "imednet.plugins.MongoDbSinkConfigProtocol",
                            "imednet.plugins.Neo4jSinkConfigProtocol",
                            "imednet.plugins.SnowflakeSinkConfigProtocol",
                        ):
                            protocol_sink_attrs += 1

                    has_proto_call = any(
                        _is_protocol_cls(ancestor) and "__call__" in ancestor.__dict__
                        for ancestor in obj.__mro__
                    )
                    if has_proto_call:
                        cid = f"{dt_id}.__call__"
                        if cid not in parsed.ids:
                            print(
                                f"Error: Callable Protocol '{dt_id}' missing '__call__' in {html_file.name}."
                            )
                            has_errors = True
                        else:
                            callable_protocols += 1

                # 5. Pydantic BaseModel fields, defaults/Required markers, and skipped __init__
                if issubclass(obj, BaseModel) and obj is not BaseModel:
                    basemodel_classes += 1
                    if "__init__" not in obj.__dict__ and f"{dt_id}.__init__" in parsed.ids:
                        print(
                            f"Error: Inherited BaseModel.__init__ must be skipped on '{dt_id}' in {html_file.name}."
                        )
                        has_errors = True
                    if not issubclass(obj, RootModel):
                        for fname in obj.model_fields:
                            if fname.startswith("_"):
                                continue
                            fid = f"{dt_id}.{fname}"
                            if fid not in parsed.ids:
                                print(
                                    f"Error: BaseModel field '{fid}' missing from {html_file.name}."
                                )
                                has_errors = True
                                continue
                            f_dt = parsed.dt_text.get(fid, "")
                            f_dl = parsed.dl_text_by_dt_id.get(fid, "")
                            if ":" not in f_dt or ("=" not in f_dt and "Required." not in f_dl):
                                print(
                                    f"Error: BaseModel field '{fid}' missing type/default/'Required.' in {html_file.name}."
                                )
                                has_errors = True
                            else:
                                basemodel_fields += 1

            elif "function" in dl_classes or "method" in dl_classes:
                resolved = _resolve_dotted_symbol(dt_id)
                if resolved is None:
                    continue
                _mod_name, raw_fn = resolved
                fn = raw_fn.__func__ if isinstance(raw_fn, (staticmethod, classmethod)) else raw_fn
                try:
                    overloads = typing.get_overloads(fn)
                except Exception:
                    overloads = []
                if overloads:
                    seen_qnames.add(dt_id)
                    dt_headers = parsed.dt_list_by_id.get(dt_id, [])
                    if len(dt_headers) < len(overloads):
                        print(
                            f"Error: Overloaded callable '{dt_id}' rendered {len(dt_headers)} <dt> signature(s), expected {len(overloads)} in {html_file.name}."
                        )
                        has_errors = True
                    elif any("(*args, **kwargs)" in hdr for hdr in dt_headers):
                        print(
                            f"Error: Overloaded callable '{dt_id}' collapsed to (*args, **kwargs) in {html_file.name}."
                        )
                        has_errors = True
                    else:
                        overload_callables += 1
                        overload_headers += len(dt_headers)

    # Full-build inventory & cross-link verification when full package reference is present
    is_full_build = (
        (api_html_dir / "imednet_sinks.html").exists()
        and (api_html_dir / "imednet_workflows.html").exists()
        and (api_html_dir / "imednet.validation.html").exists()
    )
    if is_full_build:
        if enum_classes < 15 or enum_values < 90:
            print(
                f"Error: Expected >=15 Enum classes and >=90 Enum values, got {enum_classes} classes and {enum_values} values."
            )
            has_errors = True
        if "imednet.models.jobs.JobState" not in page_by_id:
            print("Error: imednet.models.jobs.JobState missing from rendered API HTML.")
            has_errors = True

        if dataclass_classes < 25 or dataclass_fields < 224:
            print(
                f"Error: Expected >=25 @dataclass classes and >=224 fields, got {dataclass_classes} classes and {dataclass_fields} fields."
            )
            has_errors = True

        for dc_qname, expected_factory in _EXPECTED_FACTORY_DATACLASSES.items():
            dc_page = page_by_id.get(dc_qname)
            if dc_page is None:
                print(f"Error: Expected dataclass '{dc_qname}' missing from rendered API HTML.")
                has_errors = True
                continue
            dc_parsed = parsed_pages[dc_page]
            dc_sig = (
                dc_parsed.dt_text.get(f"{dc_qname}.__init__", "")
                + " "
                + dc_parsed.dl_text_by_dt_id.get(dc_qname, "")
            )
            if expected_factory not in dc_sig:
                print(
                    f"Error: Dataclass '{dc_qname}' missing readable default_factory '{expected_factory}'."
                )
                has_errors = True

        if "imednet.orchestration.types.OrchestratorResult" not in page_by_id:
            print(
                "Error: Expected TypedDict 'imednet.orchestration.types.OrchestratorResult' missing from rendered API HTML."
            )
            has_errors = True
        if typeddict_classes < 1 or typeddict_keys < 4:
            print(
                f"Error: Expected >=1 TypedDict classes and >=4 keys, got {typeddict_classes} classes and {typeddict_keys} keys."
            )
            has_errors = True

        if protocol_sink_attrs < 41:
            print(
                f"Error: Expected >=41 *SinkConfigProtocol attributes, got {protocol_sink_attrs}."
            )
            has_errors = True
        if callable_protocols < 13:
            print(
                f"Error: Expected >=13 callable Protocol.__call__ signatures, got {callable_protocols}."
            )
            has_errors = True

        for alias_qname, target_qname in _EXPECTED_PROTOCOL_ALIASES.items():
            alias_page = page_by_id.get(alias_qname)
            if alias_page is None:
                print(f"Error: Protocol TypeAlias '{alias_qname}' missing from rendered API HTML.")
                has_errors = True
                continue
            alias_parsed = parsed_pages[alias_page]
            alias_dl = alias_parsed.dl_text_by_dt_id.get(alias_qname, "")
            target_short = target_qname.rsplit(".", 1)[-1]
            if "TypeAlias" not in alias_dl or target_short not in alias_dl:
                print(
                    f"Error: Protocol TypeAlias '{alias_qname}' must render as TypeAlias referencing '{target_short}'."
                )
                has_errors = True
            hrefs = alias_parsed.internal_hrefs.get(alias_qname, [])
            if not any(h.endswith(f"#{target_qname}") for h in hrefs):
                print(
                    f"Error: Protocol TypeAlias '{alias_qname}' missing internal link to '#{target_qname}'."
                )
                has_errors = True

        if basemodel_classes < 100 or basemodel_fields < 850:
            print(
                f"Error: Expected >=100 BaseModel classes and >=850 fields, got {basemodel_classes} classes and {basemodel_fields} fields."
            )
            has_errors = True

        for ov_qname in _EXPECTED_OVERLOAD_TARGETS:
            if ov_qname not in page_by_id:
                print(
                    f"Error: Expected @overload callable '{ov_qname}' missing from rendered API HTML."
                )
                has_errors = True
        if overload_callables < 10 or overload_headers < 24:
            print(
                f"Error: Expected >=10 @overload callables and >=24 <dt> headers, got {overload_callables} callables and {overload_headers} headers."
            )
            has_errors = True

        if not has_errors:
            print(
                f"API parity gates passed ({enum_classes} Enums/{enum_values} values, "
                f"{dataclass_classes} dataclasses/{dataclass_fields} fields, "
                f"{typeddict_classes} TypedDicts/{typeddict_keys} keys, "
                f"{protocol_sink_attrs} SinkConfigProtocol attrs, {callable_protocols} callable Protocols, "
                f"{basemodel_classes} BaseModels/{basemodel_fields} fields, "
                f"{overload_callables} @overload callables/{overload_headers} headers)."
            )

    return has_errors


def check_rendered_api_reference(html_dir: Path | None = None) -> bool:
    """Verify rendered HTML model fields, endpoint methods, _modules/index.html, and [docs] backlinks."""
    print("Checking rendered Sphinx HTML API reference and viewcode backlinks...")
    repo_root = Path(__file__).resolve().parents[1]
    _ensure_workspace_src_on_path(repo_root)

    if html_dir is None:
        html_dir = repo_root / "docs" / "_build" / "html"
        if not (html_dir / "reference" / "api" / "imednet.models.html").exists():
            sphinx_bin = shutil.which("sphinx-build") or "sphinx-build"
            print(f"Building Sphinx HTML into {html_dir} for verification...")
            res = subprocess.run(
                [sphinx_bin, "-b", "html", "-W", "--keep-going", "docs", str(html_dir)],
                cwd=str(repo_root),
                check=False,
            )
            if res.returncode != 0:
                print("Error: Strict sphinx-build failed while preparing HTML for validation.")
                return True

    api_html_dir = html_dir / "reference" / "api"
    parity_failed = False
    if api_html_dir.is_dir() and any(api_html_dir.glob("*.html")):
        parity_failed = _verify_api_reference_parity(api_html_dir, repo_root)

    models_page = api_html_dir / "imednet.models.html"
    endpoints_page = api_html_dir / "imednet.endpoints.html"
    sdk_page = api_html_dir / "imednet.html"
    pagination_page = api_html_dir / "imednet.pagination.html"
    core_page = api_html_dir / "imednet.core.html"
    modules_index = html_dir / "_modules" / "index.html"

    for required_page in (
        models_page,
        endpoints_page,
        sdk_page,
        pagination_page,
        core_page,
        modules_index,
    ):
        if not required_page.exists():
            print(f"Error: Expected rendered HTML page {required_page} does not exist.")
            return True

    has_errors = parity_failed

    forbidden_internal_pages = (
        "imednet.http.html",
        "imednet.core.http.html",
        "imednet.core.operations.html",
        "imednet.core.endpoint.html",
        "imednet.core.endpoint.operations.html",
        "imednet.core.paginator.html",
        "imednet.core.retry.html",
        "imednet.core.client.html",
        "imednet.core.async_client.html",
        "imednet.core.base_client.html",
        "imednet.core.http_client_base.html",
    )
    for forbidden_name in forbidden_internal_pages:
        if (api_html_dir / forbidden_name).exists():
            print(f"Error: Internal module page '{forbidden_name}' must not be generated.")
            has_errors = True

    # 1. Verify 13 model submodules, 14 contract models (198+ fields), and 4 nested models
    from imednet.models.engine import get_contract

    contract = get_contract()
    models_parsed = _parse_html_file(models_page)
    model_submodules = (
        "codings",
        "forms",
        "intervals",
        "jobs",
        "queries",
        "record_revisions",
        "records",
        "sites",
        "studies",
        "subjects",
        "users",
        "variables",
        "visits",
    )
    nested_models = {
        "Keyword": "records",
        "QueryComment": "queries",
        "Role": "users",
        "SubjectKeyword": "subjects",
    }

    expected_models: dict[str, tuple[str, type]] = {}
    for sub in model_submodules:
        mod_anchor = f"module-imednet.models.{sub}"
        if mod_anchor not in models_parsed.ids:
            print(f"Error: Submodule anchor '{mod_anchor}' missing from imednet.models.html.")
            has_errors = True
        mod = importlib.import_module(f"imednet.models.{sub}")
        for sym in getattr(mod, "__all__", []):
            obj = getattr(mod, sym, None)
            if isinstance(obj, type) and sym in contract.models:
                expected_models[sym] = (f"imednet.models.{sub}.{sym}", obj)

    for nested_name, sub in nested_models.items():
        mod = importlib.import_module(f"imednet.models.{sub}")
        expected_models[nested_name] = (
            f"imednet.models.{sub}.{nested_name}",
            getattr(mod, nested_name),
        )

    if len(expected_models) < 18:
        print(
            f"Error: Expected 18 documented models (14 contract + 4 nested), found {len(expected_models)}."
        )
        has_errors = True

    distinct_rendered_fields: set[tuple[str, str]] = set()
    for model_name, (qname, model_cls) in sorted(expected_models.items()):
        if qname not in models_parsed.ids:
            print(f"Error: Model '{qname}' missing from imednet.models.html.")
            has_errors = True
            continue

        dt_str = models_parsed.dt_text.get(qname, "")
        dl_str = models_parsed.dl_text_by_dt_id.get(qname, "")
        if "[source]" not in dt_str:
            print(f"Error: Model '{qname}' is missing a [source] link.")
            has_errors = True

        doc_first_line = (model_cls.__doc__ or "").strip().splitlines()
        if not doc_first_line or doc_first_line[0].strip() not in dl_str:
            print(f"Error: Model '{qname}' docstring missing from imednet.models.html.")
            has_errors = True

        expected_base = "Job" if model_name == "JobStatus" else "ImednetBaseModel"
        if f"Bases: {expected_base}" not in dl_str or f"Bases: {model_name}" in dl_str:
            print(f"Error: Model '{qname}' has missing or circular Bases declaration.")
            has_errors = True

        for fname in getattr(model_cls, "model_fields", {}):
            field_id = f"{qname}.{fname}"
            if field_id not in models_parsed.ids:
                print(f"Error: Model field '{field_id}' missing from imednet.models.html.")
                has_errors = True
            elif model_name in contract.models:
                if (
                    model_name == "JobStatus"
                    and fname in contract.models["Job"].fields
                    and fname not in contract.models["JobStatus"].fields
                ):
                    continue
                distinct_rendered_fields.add((model_name, fname))

    if len(distinct_rendered_fields) < 198:
        print(
            f"Error: Expected at least 198 rendered contract model fields, got {len(distinct_rendered_fields)}."
        )
        has_errors = True

    # 2. Verify all 26 endpoints and sync/async method signatures
    from imednet.endpoints.registry import ASYNC_ENDPOINT_REGISTRY, ENDPOINT_REGISTRY

    endpoints_parsed = _parse_html_file(endpoints_page)
    all_endpoints = [*ENDPOINT_REGISTRY.values(), *ASYNC_ENDPOINT_REGISTRY.values()]
    list_count = 0
    for ep_cls in all_endpoints:
        qname = f"{ep_cls.__module__}.{ep_cls.__name__}"
        is_async = ep_cls.__name__.startswith("Async")
        if qname not in endpoints_parsed.ids:
            print(f"Error: Endpoint '{qname}' missing from imednet.endpoints.html.")
            has_errors = True
            continue

        if hasattr(ep_cls, "list"):
            list_count += 1
            for method_name in ("list", "to_dataframe"):
                mid = f"{qname}.{method_name}"
                if mid not in endpoints_parsed.ids:
                    print(f"Error: Endpoint method '{mid}' missing from imednet.endpoints.html.")
                    has_errors = True
                elif is_async:
                    if (
                        method_name == "list"
                        and "AsyncIterator" not in endpoints_parsed.dl_text_by_dt_id.get(mid, "")
                    ):
                        print(
                            f"Error: Async list method '{mid}' missing AsyncIterator return type."
                        )
                        has_errors = True
                    if method_name == "to_dataframe" and not endpoints_parsed.dt_text.get(
                        mid, ""
                    ).startswith("async "):
                        print(f"Error: Async method '{mid}' missing 'async' coroutine prefix.")
                        has_errors = True
        elif f"{qname}.list" in endpoints_parsed.ids:
            print(f"Error: Endpoint '{qname}' unexpectedly documents .list().")
            has_errors = True

        if hasattr(ep_cls, "get"):
            mid = f"{qname}.get"
            if mid not in endpoints_parsed.ids:
                print(f"Error: Endpoint method '{mid}' missing from imednet.endpoints.html.")
                has_errors = True
            elif is_async and not endpoints_parsed.dt_text.get(mid, "").startswith("async "):
                print(f"Error: Async method '{mid}' missing 'async' coroutine prefix.")
                has_errors = True
        elif f"{qname}.get" in endpoints_parsed.ids:
            print(f"Error: List-only endpoint '{qname}' unexpectedly documents .get().")
            has_errors = True

    if list_count != 24:
        print(f"Error: Expected 24 list endpoints, found {list_count}.")
        has_errors = True

    special_methods = (
        (ENDPOINT_REGISTRY["records"], "create", False),
        (ASYNC_ENDPOINT_REGISTRY["records"], "create", True),
        (ENDPOINT_REGISTRY["subjects"], "list_by_site", False),
        (ASYNC_ENDPOINT_REGISTRY["subjects"], "list_by_site", True),
        (ENDPOINT_REGISTRY["jobs"], "poll", False),
        (ASYNC_ENDPOINT_REGISTRY["jobs"], "poll", True),
        (ENDPOINT_REGISTRY["jobs"], "poll_many", False),
        (ASYNC_ENDPOINT_REGISTRY["jobs"], "poll_many", True),
    )
    for ep_cls, method_name, expect_async in special_methods:
        mid = f"{ep_cls.__module__}.{ep_cls.__name__}.{method_name}"
        if mid not in endpoints_parsed.ids:
            print(f"Error: Endpoint method '{mid}' missing from imednet.endpoints.html.")
            has_errors = True
        elif expect_async and not endpoints_parsed.dt_text.get(mid, "").startswith("async "):
            print(f"Error: Async method '{mid}' missing 'async' coroutine prefix.")
            has_errors = True

    endpoints_text = endpoints_page.read_text(encoding="utf-8")
    for forbidden_ep_token in (
        "ListOperation",
        "FilterGetOperation",
        "SyncRequestExecutor",
        "AsyncRequestExecutor",
        "model_validate",
    ):
        if forbidden_ep_token in endpoints_text:
            print(
                f"Error: Forbidden internal/inherited symbol '{forbidden_ep_token}' in imednet.endpoints.html."
            )
            has_errors = True

    if _verify_sdk_html(sdk_page):
        has_errors = True
    if _verify_pagination_html(pagination_page):
        has_errors = True
    if _verify_core_html(core_page):
        has_errors = True

    # 3. Verify _modules/index.html omits builtins.html and all [docs] backlinks resolve
    modules_index_text = modules_index.read_text(encoding="utf-8")
    if "builtins.html" in modules_index_text or (html_dir / "_modules" / "builtins.html").exists():
        print("Error: _modules/index.html or _modules/builtins.html contains 'builtins.html'.")
        has_errors = True

    parsed_pages_cache: dict[Path, _ApiHtmlExtractor] = {}

    def _get_parsed_page(path: Path) -> _ApiHtmlExtractor:
        resolved = path.resolve()
        if resolved not in parsed_pages_cache:
            parsed_pages_cache[resolved] = _parse_html_file(resolved)
        return parsed_pages_cache[resolved]

    broken_backlinks = 0
    unlabeled_backlinks = 0
    total_backlinks = 0
    for mod_html in sorted((html_dir / "_modules").rglob("*.html")):
        if mod_html.name == "index.html":
            continue
        mod_parsed = _get_parsed_page(mod_html)
        for href, aria_label in mod_parsed.backlink_labels:
            total_backlinks += 1
            rel_part, _, frag = href.partition("#")
            label_norm = (aria_label or "").strip()
            if (
                not label_norm
                or not label_norm.startswith("[docs]")
                or label_norm == "[docs]"
                or (frag and frag not in label_norm)
            ):
                print(
                    f"Error: Missing or non-specific [docs] aria-label in "
                    f"{mod_html.relative_to(html_dir)} for {href}: {aria_label!r}"
                )
                unlabeled_backlinks += 1
                has_errors = True

            target_file = (mod_html.parent / rel_part).resolve()
            if not target_file.exists():
                print(f"Error: Broken [docs] page link in {mod_html.relative_to(html_dir)}: {href}")
                broken_backlinks += 1
                has_errors = True
                continue
            if frag and frag not in _get_parsed_page(target_file).ids:
                print(f"Error: Broken [docs] fragment in {mod_html.relative_to(html_dir)}: {href}")
                broken_backlinks += 1
                has_errors = True

    if total_backlinks == 0:
        print("Error: Zero [docs] viewcode backlinks found in _modules/.")
        has_errors = True

    # 4. Verify every a.headerlink and [source] link across all built HTML pages has a target-specific aria-label
    generic_permalink_labels = {
        "Permalink to this heading",
        "Permalink to this definition",
        "Permalink to this table",
        "Permalink to this code",
        "Permalink to this image",
        "Permalink to this toctree",
        "Link to this heading",
        "Link to this definition",
        "Permalink to",
    }
    total_headerlinks = 0
    unlabeled_headerlinks = 0
    total_source_links = 0
    unlabeled_source_links = 0
    for page_html in sorted(html_dir.rglob("*.html")):
        page_parsed = _get_parsed_page(page_html)
        rel_page = page_html.relative_to(html_dir)
        for hl_href, hl_label in page_parsed.headerlinks:
            total_headerlinks += 1
            hl_norm = (hl_label or "").strip()
            if (
                not hl_norm
                or hl_norm in generic_permalink_labels
                or not hl_norm.startswith("Permalink to ")
                or not hl_norm.removeprefix("Permalink to ").strip()
            ):
                print(
                    f"Error: Missing or generic headerlink aria-label in {rel_page} "
                    f"for {hl_href}: {hl_label!r}"
                )
                unlabeled_headerlinks += 1
                has_errors = True

        for src_href, src_label, has_src_class in page_parsed.source_links:
            total_source_links += 1
            _, _, src_frag = src_href.partition("#")
            src_norm = (src_label or "").strip()
            if (
                not has_src_class
                or not src_norm
                or not src_norm.startswith("[source]")
                or src_norm == "[source]"
                or (src_frag and src_frag not in src_norm)
            ):
                print(
                    f"Error: Missing .viewcode-source-link or symbol aria-label in {rel_page} "
                    f"for {src_href}: class={has_src_class}, aria-label={src_label!r}"
                )
                unlabeled_source_links += 1
                has_errors = True

    if total_headerlinks == 0:
        print("Error: Zero a.headerlink permalinks found in rendered HTML.")
        has_errors = True
    if total_source_links == 0:
        print("Error: Zero [source] viewcode links found in rendered HTML.")
        has_errors = True

    if not has_errors:
        print(
            f"Rendered API reference verified ({len(expected_models)} models, "
            f"{len(distinct_rendered_fields)} fields, {len(all_endpoints)} endpoints, "
            f"{total_headerlinks} headerlinks, {total_source_links} [source] links, "
            f"{total_backlinks} [docs] backlinks with {broken_backlinks} broken)."
        )
    return has_errors


def check_sphinx_seo_configuration(repo_root: Path | None = None) -> bool:
    """Validate Sphinx templates_path, SEO hooks, index meta description, and orphan 404.rst."""
    print("Checking Sphinx SEO and 404 configuration...")
    root = repo_root or Path(__file__).resolve().parents[1]
    conf_py = root / "docs" / "conf.py"
    index_rst = root / "docs" / "index.rst"
    not_found_rst = root / "docs" / "404.rst"

    if not conf_py.exists():
        print(f"Error: {conf_py} not found.")
        return True

    has_errors = False
    try:
        conf_text = conf_py.read_text(encoding="utf-8")
        tree = ast.parse(conf_text, filename=str(conf_py))
    except Exception as exc:
        print(f"Error parsing {conf_py}: {exc}")
        return True

    templates_val: list[Any] | None = None
    exclude_patterns_vals: set[str] = set()
    linkcheck_ignore_vals: list[str] = []
    connected_events: set[str] = set()

    def _collect_str_constants(list_node: ast.List) -> list[str]:
        return [
            elt.value
            for elt in list_node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.List):
                    if target.id == "templates_path":
                        templates_val = node.value.elts
                    elif target.id == "exclude_patterns":
                        exclude_patterns_vals.update(_collect_str_constants(node.value))
                    elif target.id == "linkcheck_ignore":
                        linkcheck_ignore_vals.extend(_collect_str_constants(node.value))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.List)
        ):
            if node.target.id == "templates_path":
                templates_val = node.value.elts
            elif node.target.id == "exclude_patterns":
                exclude_patterns_vals.update(_collect_str_constants(node.value))
            elif node.target.id == "linkcheck_ignore":
                linkcheck_ignore_vals.extend(_collect_str_constants(node.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            connected_events.add(node.args[0].value)

    if templates_val is None or len(templates_val) != 0:
        print("Error: docs/conf.py must set templates_path = [].")
        has_errors = True

    if "_includes/**" not in exclude_patterns_vals:
        print("Error: docs/conf.py exclude_patterns must include '_includes/**'.")
        has_errors = True

    pyodide_probe_url = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js"
    for pattern in linkcheck_ignore_vals:
        try:
            if re.match(pattern, pyodide_probe_url):
                print(
                    f"Error: linkcheck_ignore pattern '{pattern}' suppresses Pyodide CDN validation."
                )
                has_errors = True
        except re.error as exc:
            print(f"Error: Invalid regex '{pattern}' in linkcheck_ignore: {exc}")
            has_errors = True

    for event_name in ("html-page-context", "build-finished"):
        if event_name not in connected_events:
            print(f"Error: docs/conf.py setup() must connect '{event_name}'.")
            has_errors = True

    if not index_rst.exists():
        print(f"Error: {index_rst} not found.")
        has_errors = True
    else:
        index_text = index_rst.read_text(encoding="utf-8")
        if ".. meta::" not in index_text or ":description:" not in index_text:
            print("Error: docs/index.rst must declare '.. meta::' with ':description:'.")
            has_errors = True

    if not not_found_rst.exists():
        print(f"Error: {not_found_rst} not found.")
        has_errors = True
    else:
        not_found_text = not_found_rst.read_text(encoding="utf-8")
        first_non_empty = next(
            (line.strip() for line in not_found_text.splitlines() if line.strip()),
            "",
        )
        if first_non_empty != ":orphan:":
            print("Error: docs/404.rst must begin with ':orphan:'.")
            has_errors = True

    if not has_errors:
        print("Sphinx SEO and 404 configuration is valid.")
    return has_errors


def check_built_html_seo(
    html_dir: Path | None = None,
    *,
    require_exists: bool = False,
    expected_baseurl: str = "https://fderuiter.github.io/imednet-toolkit/",
) -> bool:
    """Verify sitemap.xml, robots.txt, 404.html asset paths, social metadata, and scoped JS across built HTML."""
    target_dir = html_dir or DEFAULT_HTML_DIR
    if not target_dir.exists():
        if require_exists:
            print(f"Error: Built HTML directory not found at {target_dir}.")
            return True
        print(f"Skipping rendered HTML SEO check ({target_dir} not yet built).")
        return False

    print(f"Checking rendered HTML SEO and Pages artifacts in {target_dir}...")
    has_errors = False
    normalized_base = expected_baseurl.rstrip("/") + "/"

    sitemap_path = target_dir / "sitemap.xml"
    if not sitemap_path.exists():
        print(f"Error: Missing {sitemap_path}.")
        has_errors = True
    else:
        try:
            root_el = DefusedET.fromstring(sitemap_path.read_text(encoding="utf-8"))
            ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
            if root_el.tag != f"{ns}urlset":
                print(f"Error: Unexpected root tag in sitemap.xml: {root_el.tag}")
                has_errors = True
            locs = [
                (loc_el.text or "").strip()
                for loc_el in root_el.findall(f"{ns}url/{ns}loc")
                if (loc_el.text or "").strip()
            ]
            if not locs:
                print("Error: sitemap.xml contains zero <loc> entries.")
                has_errors = True
            if normalized_base not in locs:
                print(f"Error: sitemap.xml is missing canonical root URL '{normalized_base}'.")
                has_errors = True
            if any(loc.endswith("/404.html") for loc in locs):
                print("Error: sitemap.xml must not index 404.html.")
                has_errors = True
            if any("/_includes/" in loc for loc in locs):
                print("Error: sitemap.xml must not index partial '_includes/' URLs.")
                has_errors = True
            if any(not loc.startswith(normalized_base) for loc in locs):
                print(f"Error: sitemap.xml contains URLs outside '{normalized_base}'.")
                has_errors = True
        except Exception as exc:
            print(f"Error parsing {sitemap_path}: {exc}")
            has_errors = True

    robots_path = target_dir / "robots.txt"
    if not robots_path.exists():
        print(f"Error: Missing {robots_path}.")
        has_errors = True
    else:
        robots_text = robots_path.read_text(encoding="utf-8")
        expected_sitemap_line = f"Sitemap: {normalized_base}sitemap.xml"
        if "User-agent: *" not in robots_text or expected_sitemap_line not in robots_text:
            print(f"Error: {robots_path} is missing User-agent or '{expected_sitemap_line}'.")
            has_errors = True

    not_found_html = target_dir / "404.html"
    if not not_found_html.exists():
        print(f"Error: Missing {not_found_html}.")
        has_errors = True
    else:
        nf_text = not_found_html.read_text(encoding="utf-8")
        if "/imednet-toolkit/_static/" not in nf_text:
            print("Error: 404.html does not use '/imednet-toolkit/_static/' asset paths.")
            has_errors = True
        if re.search(r'(?:href|src)=["\'](?:\./)?_static/', nf_text):
            print("Error: 404.html still contains relative '_static/' asset references.")
            has_errors = True
        if 'id="imednet-pyodide-config"' in nf_text:
            print("Error: 404.html must omit '#imednet-pyodide-config' metadata.")
            has_errors = True

    html_files = sorted(target_dir.rglob("*.html"))
    if not html_files:
        print(f"Error: No .html files found in {target_dir}.")
        has_errors = True

    required_meta_patterns = (
        ('<meta name="description"', r'<meta\s+[^>]*name="description"[^>]*>'),
        ('property="og:title"', r'<meta\s+[^>]*property="og:title"[^>]*>'),
        ('property="og:description"', r'<meta\s+[^>]*property="og:description"[^>]*>'),
        ('property="og:url"', r'<meta\s+[^>]*property="og:url"[^>]*>'),
        ('property="og:image"', r'<meta\s+[^>]*property="og:image"[^>]*>'),
        ('name="twitter:card"', r'<meta\s+[^>]*name="twitter:card"[^>]*>'),
    )
    title_tag_re = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
    social_title_re = re.compile(
        r'<meta\s+[^>]*(?:property="og:title"|name="twitter:title")[^>]*content="([^"]*)"',
        re.IGNORECASE,
    )
    escaped_og_re = re.compile(
        r"&(?:amp;)?lt;\s*meta\s+[^>]*(?:property=[\"']?og:|name=[\"']?twitter:)",
        re.IGNORECASE,
    )

    for html_file in html_files:
        rel = html_file.relative_to(target_dir)
        if "_includes" in rel.parts:
            print(f"Error: Partial file built as standalone HTML: {rel}.")
            has_errors = True

        html_text = html_file.read_text(encoding="utf-8")
        if escaped_og_re.search(html_text):
            print(f"Error: {rel} contains HTML-escaped OpenGraph/social meta tags.")
            has_errors = True

        for label, pattern in required_meta_patterns:
            if not re.search(pattern, html_text):
                print(f"Error: {rel} is missing required metadata tag ({label}).")
                has_errors = True

        title_match = title_tag_re.search(html_text)
        title_val = (title_match.group(1) if title_match else "").lower()
        if "<no title>" in title_val or "&lt;no title&gt;" in title_val:
            print(f"Error: {rel} contains '<no title>' in <title>.")
            has_errors = True

        for social_title in social_title_re.findall(html_text):
            lower_social = social_title.lower()
            if "<no title>" in lower_social or "&lt;no title&gt;" in lower_social:
                print(f"Error: {rel} contains '<no title>' in social title metadata.")
                has_errors = True

        rel_posix = rel.as_posix()
        if "showcase.js" in html_text and rel_posix != "index.html":
            print(f"Error: {rel} loads 'showcase.js' (expected only on index.html).")
            has_errors = True

        has_pyodide_btn = 'data-pyodide-launch="true"' in html_text
        if not has_pyodide_btn:
            if 'id="imednet-pyodide-config"' in html_text:
                print(f"Error: {rel} includes '#imednet-pyodide-config' without a pyodide-button.")
                has_errors = True
            if "thebe_pyodide.js" in html_text:
                print(f"Error: {rel} loads 'thebe_pyodide.js' without a pyodide-button.")
                has_errors = True
        elif 'id="imednet-pyodide-config"' not in html_text or "thebe_pyodide.js" not in html_text:
            print(
                f"Error: {rel} renders pyodide-button without '#imednet-pyodide-config' and 'thebe_pyodide.js'."
            )
            has_errors = True

    if not has_errors:
        print(
            f"Rendered HTML SEO check passed ({len(html_files)} HTML page(s), sitemap.xml, robots.txt, and 404.html verified)."
        )
    return has_errors


_IMEDNET_ENV_NAME_RE = re.compile(r"^IMEDNET_[A-Z0-9_]+$")
_DOCUMENTED_IMEDNET_ENV_RE = re.compile(r"\b(IMEDNET_[A-Z0-9_]+)\b")


def _is_os_environ_expr(node: ast.AST) -> bool:
    """Return True if AST node represents ``os.environ`` or ``environ``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ) or (isinstance(node, ast.Name) and node.id == "environ")


def _resolve_env_expr(expr: ast.AST, const_map: dict[str, str]) -> list[str]:
    """Resolve an AST expression to matching ``IMEDNET_*`` environment variable names."""
    if (
        isinstance(expr, ast.Constant)
        and isinstance(expr.value, str)
        and _IMEDNET_ENV_NAME_RE.match(expr.value)
    ):
        return [expr.value]
    if isinstance(expr, ast.Name) and expr.id in const_map:
        return [const_map[expr.id]]
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        items: list[str] = []
        for elt in expr.elts:
            items.extend(_resolve_env_expr(elt, const_map))
        return items
    return []


def _extract_runtime_env_vars(packages_dir: Path) -> set[str]:
    """Extract runtime ``IMEDNET_*`` environment variables read across ``packages/*/src/**/*.py``."""
    discovered: set[str] = set()
    if not packages_dir.exists():
        return discovered

    for py_file in sorted(packages_dir.glob("*/src/**/*.py")):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (OSError, SyntaxError):
            continue

        const_map: dict[str, str] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                if isinstance(stmt.value.value, str) and _IMEDNET_ENV_NAME_RE.match(
                    stmt.value.value
                ):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            const_map[target.id] = stmt.value.value
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
                and _IMEDNET_ENV_NAME_RE.match(stmt.value.value)
            ):
                const_map[stmt.target.id] = stmt.value.value

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                is_os_getenv = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ) or (isinstance(func, ast.Name) and func.id == "getenv")
                is_environ_method = (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"get", "pop", "setdefault"}
                    and _is_os_environ_expr(func.value)
                )
                if is_os_getenv or is_environ_method:
                    discovered.update(_resolve_env_expr(node.args[0], const_map))
            elif isinstance(node, ast.Subscript) and _is_os_environ_expr(node.value):
                discovered.update(_resolve_env_expr(node.slice, const_map))
            elif isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                if any(_is_os_environ_expr(op) for op in operands):
                    for op in operands:
                        if not _is_os_environ_expr(op):
                            discovered.update(_resolve_env_expr(op, const_map))
            elif isinstance(node, ast.For):
                candidates = _resolve_env_expr(node.iter, const_map)
                if candidates and any(
                    isinstance(sub, ast.Attribute) and sub.attr in {"environ", "getenv"}
                    for sub in ast.walk(node)
                ):
                    discovered.update(candidates)

    return discovered


def check_env_vars_documented(repo_root: Path | None = None) -> bool:
    """Verify every runtime ``IMEDNET_*`` environment variable in ``packages/*/src`` is in ``configuration.rst``."""
    print("Checking runtime IMEDNET_* environment variables against configuration.rst...")
    root = repo_root or REPO_ROOT
    packages_dir = root / "packages"
    config_rst = root / "docs" / "reference" / "configuration.rst"
    if not packages_dir.is_dir() or not config_rst.is_file():
        print(
            f"Error: Missing packages directory ({packages_dir}) or configuration reference ({config_rst})."
        )
        return True

    runtime_vars = _extract_runtime_env_vars(packages_dir)
    documented_vars = set(
        _DOCUMENTED_IMEDNET_ENV_RE.findall(config_rst.read_text(encoding="utf-8"))
    )
    missing = sorted(runtime_vars - documented_vars)
    if missing:
        for var_name in missing:
            print(
                f"Error: Runtime environment variable '{var_name}' is undocumented in {config_rst}."
            )
        return True

    print(
        f"Environment variable parity verified ({len(runtime_vars)} runtime IMEDNET_* variable(s) documented)."
    )
    return False


def check_sdtm_and_phi_parity(repo_root: Path | None = None) -> bool:
    """Verify all SDTM_DOMAIN_SPECS domains, SensitivityRegistry keys, and SinkConfig controls are in record_mapping.rst."""
    print("Checking SDTM domains and SensitivityRegistry keys against record_mapping.rst...")
    root = repo_root or REPO_ROOT
    core_src = str(REPO_ROOT / "packages" / "core" / "src")
    if os.environ.get("IMEDNET_TEST_WHEELS") != "1" and core_src not in sys.path:
        sys.path.insert(0, core_src)

    record_mapping_rst = root / "docs" / "how-to" / "record_mapping.rst"
    if not record_mapping_rst.is_file():
        print(f"Error: Missing record mapping guide at {record_mapping_rst}.")
        return True

    from imednet.models.sdtm import SDTM_DOMAIN_SPECS
    from imednet.utils.security import SensitivityRegistry

    content = record_mapping_rst.read_text(encoding="utf-8")
    registry = SensitivityRegistry()
    has_errors = False

    for domain_code in sorted(SDTM_DOMAIN_SPECS):
        if f"``{domain_code}``" not in content:
            print(
                f"Error: SDTM domain '{domain_code}' from SDTM_DOMAIN_SPECS is undocumented in {record_mapping_rst}."
            )
            has_errors = True

    for phi_key in sorted(registry._sensitive_keys):
        if f"``{phi_key}``" not in content:
            print(
                f"Error: Default PHI key '{phi_key}' from SensitivityRegistry is undocumented in {record_mapping_rst}."
            )
            has_errors = True

    for exempt_key in sorted(registry._exempt_keys):
        if f"``{exempt_key}``" not in content:
            print(
                f"Error: Exempt structural key '{exempt_key}' from SensitivityRegistry is undocumented in {record_mapping_rst}."
            )
            has_errors = True

    required_sink_controls = (
        "as_sdtm",
        "sdtm_domain",
        "standards_profile",
        "min_standards_score",
        "naming_convention",
    )
    for ctrl in required_sink_controls:
        if f"``{ctrl}``" not in content:
            print(
                f"Error: SinkConfig SDTM/standards control '{ctrl}' is undocumented in {record_mapping_rst}."
            )
            has_errors = True

    if not has_errors:
        print(
            f"SDTM and PHI parity verified ({len(SDTM_DOMAIN_SPECS)} domains, "
            f"{len(registry._sensitive_keys)} sensitive keys, {len(registry._exempt_keys)} exempt keys)."
        )
    return has_errors


_STALE_SCHEMA_CACHE_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("BaseSchemaCache returns early claim", "returns early"),
    ("BaseSchemaCache skips required policies claim", "skipping required"),
    (
        "SchemaCache structural and scalar type checks only claim",
        "structural and scalar type checks only",
    ),
    (
        "SchemaCache basic scalar types only claim",
        "which only checks form existence, unknown variables, and basic scalar types",
    ),
)


def _find_stale_schema_cache_claims(guide_text: str) -> list[str]:
    """Return labels for stale ``SchemaCache`` claims using linear line/token scans."""
    claims: list[str] = []
    normalized_text = " ".join(guide_text.split()).lower()
    for label, phrase in _STALE_SCHEMA_CACHE_SUBSTRINGS:
        if phrase in normalized_text:
            claims.append(label)

    lines = guide_text.splitlines()
    for idx, line in enumerate(lines):
        norm_line = " ".join(line.strip().split()).lower()
        if (
            norm_line.startswith("* - required variable policies")
            and idx + 1 < len(lines)
            and " ".join(lines[idx + 1].strip().split()).lower().startswith("- skipped")
        ):
            claims.append("Required Variable policies marked Skipped in coverage table")
            break

    idx = 0
    while idx < len(lines):
        raw_line = lines[idx]
        stripped = raw_line.lstrip(" \t")
        norm_line = " ".join(stripped.split()).lower()
        if norm_line.startswith("- **skipped by**"):
            bullet_indent = len(raw_line) - len(stripped)
            bullet_parts = [norm_line]
            idx += 1
            while idx < len(lines):
                cont_raw = lines[idx]
                cont_stripped = cont_raw.lstrip(" \t")
                if not cont_stripped:
                    break
                cont_indent = len(cont_raw) - len(cont_stripped)
                if cont_indent <= bullet_indent:
                    break
                bullet_parts.append(" ".join(cont_stripped.split()).lower())
                idx += 1
            if "required variable" in " ".join(bullet_parts):
                claims.append("Skipped by bullet listing Required Variable policies")
                break
        else:
            idx += 1

    return claims


def _extract_dunder_all_from_file(init_py: Path) -> set[str] | None:
    """Extract string literals from ``__all__ = [...]`` in ``init_py`` when present."""
    if not init_py.is_file():
        return None
    try:
        tree = ast.parse(init_py.read_text(encoding="utf-8"), filename=str(init_py))
    except SyntaxError:
        return set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            return {
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
    return set()


def check_clinical_guides_parity(repo_root: Path | None = None) -> bool:
    """Verify BaseSchemaCache policies, ODM RangeCheck/regex bounds, datadict exports, and EnrichmentPipeline scoping."""
    print(
        "Checking clinical guides parity against runtime validation, datadict, and enrichment contracts..."
    )
    root = repo_root or REPO_ROOT
    core_src = str(REPO_ROOT / "packages" / "core" / "src")
    if os.environ.get("IMEDNET_TEST_WHEELS") != "1" and core_src not in sys.path:
        sys.path.insert(0, core_src)

    schema_val_rst = root / "docs" / "explanation" / "schema_validation.rst"
    datadict_rst = root / "docs" / "how-to" / "data_dictionary.rst"
    caching_rst = root / "docs" / "how-to" / "caching.rst"
    record_mapping_rst = root / "docs" / "how-to" / "record_mapping.rst"

    for required_guide in (schema_val_rst, datadict_rst, caching_rst, record_mapping_rst):
        if not required_guide.is_file():
            print(f"Error: Missing clinical guide for parity validation: {required_guide}.")
            return True

    import imednet.datadict as datadict_pkg
    from imednet.datadict.logic import (
        _MAX_REGEX_INPUT_LENGTH,
        _MAX_REGEX_PATTERN_LENGTH,
        _MAX_REGEX_REPEAT_COUNT,
        _VALID_RANGE_COMPARATORS,
    )
    from imednet.integrations.enrichment import EnrichmentPipeline
    from imednet.utils.tabular import MASKED_SENTINEL

    has_errors = False

    expected_datadict_exports = ("RangeCheckConstraint", "validate_regex_pattern")
    runtime_exports = set(getattr(datadict_pkg, "__all__", ()))
    local_init = root / "packages" / "core" / "src" / "imednet" / "datadict" / "__init__.py"
    parsed_exports = _extract_dunder_all_from_file(local_init)
    effective_exports = parsed_exports if parsed_exports is not None else runtime_exports
    for sym in expected_datadict_exports:
        if sym not in effective_exports or not hasattr(datadict_pkg, sym):
            print(
                f"Error: Symbol '{sym}' must be exported in imednet.datadict.__all__ and importable from imednet.datadict."
            )
            has_errors = True

    schema_val_text = schema_val_rst.read_text(encoding="utf-8")
    datadict_text = datadict_rst.read_text(encoding="utf-8")
    caching_text = caching_rst.read_text(encoding="utf-8")
    record_mapping_text = record_mapping_rst.read_text(encoding="utf-8")

    for guide_path, guide_text in (
        (schema_val_rst, schema_val_text),
        (datadict_rst, datadict_text),
        (caching_rst, caching_text),
    ):
        for req_token in ("BaseSchemaCache", "Yes", "Auto Query"):
            if req_token not in guide_text:
                print(
                    f"Error: Required BaseSchemaCache policy token '{req_token}' missing from {guide_path}."
                )
                has_errors = True
        for label in _find_stale_schema_cache_claims(guide_text):
            print(f"Error: Stale SchemaCache documentation ({label}) found in {guide_path}.")
            has_errors = True

    for schema_token in (
        f"``{MASKED_SENTINEL}``",
        "masked_phi",
        "``_retired_form_variables``",
        "``deleted``",
        "``disabled``",
    ):
        if schema_token not in schema_val_text:
            print(
                f"Error: Runtime validation token '{schema_token}' is undocumented in {schema_val_rst}."
            )
            has_errors = True

    for sym in expected_datadict_exports:
        if sym not in datadict_text:
            print(f"Error: Exported symbol '{sym}' is undocumented in {datadict_rst}.")
            has_errors = True

    for comparator in sorted(_VALID_RANGE_COMPARATORS):
        if f"``{comparator}``" not in datadict_text:
            print(
                f"Error: ODM RangeCheck comparator '{comparator}' is undocumented in {datadict_rst}."
            )
            has_errors = True

    for odm_token in (
        "``Soft``",
        "``Hard``",
        "range_check_soft",
        "range_check",
        "UTC",
        "``MM-DD-YYYY``",
        "``YYYY-MM-DD``",
        "unsafe_regex",
    ):
        if odm_token not in datadict_text:
            print(
                f"Error: Data Dictionary runtime token '{odm_token}' missing from {datadict_rst}."
            )
            has_errors = True

    for cap_label, cap_value in (
        ("_MAX_REGEX_PATTERN_LENGTH", _MAX_REGEX_PATTERN_LENGTH),
        ("_MAX_REGEX_REPEAT_COUNT", _MAX_REGEX_REPEAT_COUNT),
        ("_MAX_REGEX_INPUT_LENGTH", _MAX_REGEX_INPUT_LENGTH),
    ):
        if f"``{cap_value}``" not in datadict_text:
            print(
                f"Error: Runtime regex bound {cap_label}={cap_value} is undocumented in {datadict_rst}."
            )
            has_errors = True

    process_params = inspect.signature(EnrichmentPipeline.process).parameters
    for param_name in ("domain", "form_key"):
        if param_name not in process_params:
            print(
                f"Error: EnrichmentPipeline.process missing expected runtime parameter '{param_name}'."
            )
            has_errors = True

    for enrichment_token in (
        "``(domain, source_form_key, source_variable_name)``",
        "``fallback_value``",
        "``domain.variable``",
        'domain="AE"',
        'form_key="F_AE"',
    ):
        if enrichment_token not in record_mapping_text:
            print(
                f"Error: EnrichmentPipeline scoping/terminology token '{enrichment_token}' missing from {record_mapping_rst}."
            )
            has_errors = True

    if not has_errors:
        print(
            "Clinical guides parity verified ("
            f"regex bounds={_MAX_REGEX_PATTERN_LENGTH}/{_MAX_REGEX_REPEAT_COUNT}/{_MAX_REGEX_INPUT_LENGTH}, "
            f"{len(_VALID_RANGE_COMPARATORS)} ODM comparators, {len(expected_datadict_exports)} datadict exports)."
        )
    return has_errors


_DUCKDB_CREATE_REL_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)
_STALE_RECORD_REVISIONS_TABLE_RE = re.compile(
    r"``record_revisions``\s+tables?\b",
    re.IGNORECASE,
)


def check_duckdb_relations_parity(repo_root: Path | None = None) -> bool:
    """Verify DuckDB relation names in audit_compliance.rst and CI audit commands in security_exceptions.rst."""
    print("Checking DuckDB relation names and security audit commands...")
    root = repo_root or REPO_ROOT
    centralizer_py = (
        root
        / "packages"
        / "plugins-workflows"
        / "src"
        / "imednet_workflows"
        / "duckdb_centralizer.py"
    )
    audit_rst = root / "docs" / "how-to" / "audit_compliance.rst"
    security_rst = root / "docs" / "reference" / "security_exceptions.rst"

    for required_path in (centralizer_py, audit_rst, security_rst):
        if not required_path.is_file():
            print(f"Error: Missing required file for parity validation: {required_path}.")
            return True

    centralizer_text = centralizer_py.read_text(encoding="utf-8")
    expected_relations = set(_DUCKDB_CREATE_REL_RE.findall(centralizer_text))
    if not expected_relations:
        print(f"Error: Could not extract any DuckDB relations from {centralizer_py}.")
        return True

    audit_text = audit_rst.read_text(encoding="utf-8")
    has_errors = False

    for rel_name in sorted(expected_relations):
        if f"``{rel_name}``" not in audit_text:
            print(
                f"Error: DuckDB relation '{rel_name}' from duckdb_centralizer.py is missing from {audit_rst}."
            )
            has_errors = True

    if "silver_records_history" in expected_relations:
        for scd_col in ("valid_from", "valid_to", "is_current"):
            if f"``{scd_col}``" not in audit_text:
                print(
                    f"Error: SCD Type 2 column '{scd_col}' from silver_records_history is missing from {audit_rst}."
                )
                has_errors = True

    if _STALE_RECORD_REVISIONS_TABLE_RE.search(audit_text):
        print(
            f"Error: Stale 'record_revisions' DuckDB table reference found in {audit_rst} "
            "(expected 'bronze_revisions' table and 'record_revisions' view)."
        )
        has_errors = True

    security_text = security_rst.read_text(encoding="utf-8")
    if "lint:audit" in security_text:
        print(
            f"Error: Stale 'lint:audit' command found in {security_rst}; expected locked uv export + pip-audit."
        )
        has_errors = True
    if "uv export" not in security_text or "pip-audit" not in security_text:
        print(f"Error: {security_rst} must document the CI 'uv export' + 'pip-audit' workflow.")
        has_errors = True

    if not has_errors:
        print(f"DuckDB relation and security audit parity verified ({sorted(expected_relations)}).")
    return has_errors


class _ComplianceLinkParser(HTMLParser):
    """Parse HTML page anchors, internal links, and unlinked Sphinx Python cross-references."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.internal_links: list[tuple[str, str]] = []
        self.unlinked_py_xrefs: list[str] = []
        self._active_href: str | None = None
        self._link_text_parts: list[str] = []
        self._in_unlinked_xref = False
        self._xref_depth = 0
        self._xref_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        elem_id = attr_map.get("id")
        if elem_id:
            self.ids.add(elem_id)

        classes = set((attr_map.get("class") or "").split())
        if tag == "a" and "reference" in classes and "internal" in classes:
            href = attr_map.get("href")
            if href:
                self._active_href = href
                self._link_text_parts = []

        if self._in_unlinked_xref:
            self._xref_depth += 1
        elif tag == "code" and "xref" in classes and "py" in classes and self._active_href is None:
            self._in_unlinked_xref = True
            self._xref_depth = 1
            self._xref_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._link_text_parts.append(data)
        if self._in_unlinked_xref:
            self._xref_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_href is not None:
            self.internal_links.append((self._active_href, "".join(self._link_text_parts).strip()))
            self._active_href = None
            self._link_text_parts = []
        if self._in_unlinked_xref:
            self._xref_depth -= 1
            if self._xref_depth <= 0:
                text = "".join(self._xref_text_parts).strip()
                if text:
                    self.unlinked_py_xrefs.append(text)
                self._in_unlinked_xref = False
                self._xref_text_parts = []


_FIRST_PARTY_XREF_PREFIXES = (
    "imednet",
    "apache_airflow_providers_imednet",
    "RecordRevision",
    "ConfigVersionStore",
    "DuckDBIngestionWorkflow",
    "OIDCAuth",
    "AuthStrategy",
    "ApiKeyAuth",
    "CredentialSource",
    "MemoryCredentialStore",
    "Credentials",
    "StandardsReadinessValidator",
    "SinkConfig",
    "ExportSink",
    "transform_records_to_sdtm_rows",
    "evaluate_standards_readiness",
    "RangeCheckConstraint",
    "validate_regex_pattern",
    "BaseSchemaCache",
    "SchemaCache",
    "AsyncSchemaCache",
    "EnrichmentPipeline",
    "validate_record_data",
)


def check_compliance_html_links(html_dir: Path | None, *, require_exists: bool = False) -> bool:
    """Verify compliance/SPI cross-references resolve to valid HTML files and anchor IDs."""
    if html_dir is None:
        return False

    print(f"Checking compliance and SPI HTML cross-references in {html_dir}...")
    if not html_dir.exists():
        if require_exists:
            print(f"Error: Rendered HTML directory {html_dir} does not exist.")
            return True
        print(f"Skipping compliance HTML link check ({html_dir} not built yet).")
        return False

    required_page_symbols: dict[str, tuple[str, ...]] = {
        "how-to/audit_compliance.html": ("RecordRevision.validate_chain",),
        "explanation/architecture.html": (
            "transform_records_to_sdtm_rows",
            "evaluate_standards_readiness",
        ),
        "how-to/record_mapping.html": (
            "transform_records_to_sdtm_rows",
            "evaluate_standards_readiness",
            "EnrichmentPipeline",
        ),
        "how-to/data_dictionary.html": (
            "RangeCheckConstraint",
            "validate_regex_pattern",
        ),
        "explanation/schema_validation.html": (
            "BaseSchemaCache",
            "validate_record_data",
        ),
        "reference/governance.html": ("rollback_config",),
        "explanation/api_overview.html": ("AuthStrategy", "OIDCAuth", "MemoryCredentialStore"),
    }

    models_html = html_dir / "reference" / "api" / "imednet.models.html"
    datadict_html = html_dir / "reference" / "api" / "imednet.datadict.html"
    for api_ref_html in (models_html, datadict_html):
        if not api_ref_html.is_file():
            print(f"Error: Missing rendered API reference page {api_ref_html}.")
            return True

    parsed_cache: dict[Path, _ComplianceLinkParser] = {}

    def _get_parsed(page_path: Path) -> _ComplianceLinkParser:
        resolved = page_path.resolve()
        if resolved not in parsed_cache:
            parser = _ComplianceLinkParser()
            parser.feed(resolved.read_text(encoding="utf-8"))
            parsed_cache[resolved] = parser
        return parsed_cache[resolved]

    has_errors = False
    models_parsed = _get_parsed(models_html)
    validate_chain_id = "imednet.models.record_revisions.RecordRevision.validate_chain"
    if validate_chain_id not in models_parsed.ids:
        print(f"Error: Method anchor '{validate_chain_id}' missing from {models_html}.")
        has_errors = True

    datadict_parsed = _get_parsed(datadict_html)
    for datadict_anchor in (
        "imednet.datadict.logic.RangeCheckConstraint",
        "imednet.datadict.logic.validate_regex_pattern",
    ):
        if datadict_anchor not in datadict_parsed.ids:
            print(f"Error: Datadict API anchor '{datadict_anchor}' missing from {datadict_html}.")
            has_errors = True

    for rel_page, expected_symbols in required_page_symbols.items():
        page_path = html_dir / rel_page
        if not page_path.is_file():
            print(f"Error: Expected rendered guide {page_path} does not exist.")
            has_errors = True
            continue

        parsed = _get_parsed(page_path)

        for unlinked in parsed.unlinked_py_xrefs:
            if unlinked.startswith(_FIRST_PARTY_XREF_PREFIXES):
                print(f"Error: Unresolved Python cross-reference '{unlinked}' on {rel_page}.")
                has_errors = True

        for sym in expected_symbols:
            matching_hrefs = [href for href, text in parsed.internal_links if sym in text]
            if not matching_hrefs:
                print(f"Error: Expected linked cross-reference '{sym}' missing from {rel_page}.")
                has_errors = True

        for href, link_text in parsed.internal_links:
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            rel_part, _, frag = href.partition("#")
            target_file = (
                (page_path.parent / rel_part).resolve() if rel_part else page_path.resolve()
            )
            if not target_file.is_file():
                print(
                    f"Error: Broken internal link '{href}' ({link_text}) on {rel_page}: target file missing."
                )
                has_errors = True
                continue
            if frag and frag not in _get_parsed(target_file).ids:
                print(
                    f"Error: Broken internal anchor '{href}' ({link_text}) on {rel_page}: '#{frag}' missing."
                )
                has_errors = True

    if not has_errors:
        print(
            f"Compliance and SPI HTML cross-references verified across {len(required_page_symbols)} guide(s)."
        )
    return has_errors


DEFAULT_HTML_DIR = REPO_ROOT / "docs" / "_build" / "html"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for documentation validation."""
    parser = argparse.ArgumentParser(description="Validate documentation configuration and SEO.")
    parser.add_argument(
        "--html-dir",
        type=Path,
        default=DEFAULT_HTML_DIR,
        help="Path to rendered Sphinx HTML directory (default: docs/_build/html).",
    )
    parser.add_argument(
        "--require-html",
        action="store_true",
        help="Fail if the rendered HTML directory does not exist.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Run validation scripts."""
    args = parse_args(argv)
    todos_failed = check_todos_in_init()
    extras_failed = check_documented_extras()
    cli_failed = check_cli_snippets()
    packages_failed = check_packages_registered()
    docs_failed = check_package_docs()
    governance_failed = check_governance_and_issue_templates()
    pypi_tags_failed = check_pypi_install_tags()
    release_please_failed = check_release_please_parity(
        html_dir=args.html_dir,
        require_html=args.require_html,
    )
    examples_failed = check_examples_index_parity()
    sidebars_failed = check_diataxis_sidebar_isolation()
    adr_failed = check_adr_records()
    env_vars_failed = check_env_vars_documented()
    sdtm_phi_failed = check_sdtm_and_phi_parity()
    clinical_guides_failed = check_clinical_guides_parity()
    duckdb_parity_failed = check_duckdb_relations_parity()
    githubpages_failed = check_sphinx_githubpages_extension()
    baseurl_failed = check_sphinx_html_baseurl()
    seo_config_failed = check_sphinx_seo_configuration()
    suppression_failed = check_no_duplicate_suppression()
    typehints_dep_failed = check_no_autodoc_typehints_dependency()
    api_html_dir = args.html_dir if args.require_html or args.html_dir != DEFAULT_HTML_DIR else None
    api_html_failed = check_rendered_api_reference(api_html_dir)
    compliance_links_failed = check_compliance_html_links(
        api_html_dir, require_exists=args.require_html
    )
    routes_failed = check_readme_github_pages_routes(
        args.html_dir,
        require_exists=args.require_html,
    )
    html_seo_failed = check_built_html_seo(args.html_dir, require_exists=args.require_html)

    if (
        todos_failed
        or extras_failed
        or cli_failed
        or packages_failed
        or docs_failed
        or governance_failed
        or pypi_tags_failed
        or release_please_failed
        or examples_failed
        or sidebars_failed
        or adr_failed
        or env_vars_failed
        or sdtm_phi_failed
        or clinical_guides_failed
        or duckdb_parity_failed
        or githubpages_failed
        or baseurl_failed
        or seo_config_failed
        or suppression_failed
        or typehints_dep_failed
        or api_html_failed
        or compliance_links_failed
        or routes_failed
        or html_seo_failed
    ):
        print("Validation failed.")
        sys.exit(1)

    print("Validation passed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
