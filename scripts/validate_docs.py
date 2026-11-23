"""Validation script for documentation."""

import argparse
import ast
import contextlib
import dataclasses
import functools
import importlib
import inspect
import io
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import typing
from collections.abc import Sequence
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as DefusedET


def check_todos_in_init(roots: Sequence[Path] | None = None) -> bool:
    """Ensure no TODO strings exist in module, class, or function docstrings across examples/ and packages/*/src/."""
    print("Checking for TODOs in docstrings across examples/ and packages/*/src/...")
    if roots is None:
        candidate_roots = [Path("examples"), *sorted(Path("packages").glob("*/src"))]
    else:
        candidate_roots = list(roots)

    has_todos = False
    for base_path in candidate_roots:
        if not base_path.exists():
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


def check_documented_extras(repo_root: Path = Path(".")) -> bool:
    """Validate that every documented <package>[<extra>] reference matches a declared extra in pyproject.toml."""
    print("Checking documented package extras against pyproject.toml declarations...")
    pkg_extras = _load_declared_package_extras(repo_root)
    if not pkg_extras:
        print(f"Warning: No pyproject.toml package definitions found under {repo_root}.")
        return False

    scan_candidates: list[Path] = []
    root_readme = repo_root / "README.md"
    if root_readme.is_file():
        scan_candidates.append(root_readme)
    packages_dir = repo_root / "packages"
    if packages_dir.exists():
        scan_candidates.extend(sorted(packages_dir.glob("*/README.md")))
        scan_candidates.extend(sorted(packages_dir.glob("*/src/**/*.py")))
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        scan_candidates.extend(sorted(docs_dir.rglob("*.rst")))
        scan_candidates.extend(sorted(docs_dir.rglob("*.md")))
    examples_dir = repo_root / "examples"
    if examples_dir.exists():
        scan_candidates.extend(sorted(examples_dir.rglob("*.py")))
        scan_candidates.extend(sorted(examples_dir.rglob("*.md")))

    has_errors = False
    seen: set[Path] = set()
    for path in scan_candidates:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        rel_parts = path.relative_to(repo_root).parts
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


def check_cli_snippets(repo_root: Path = Path(".")) -> bool:
    """Parse documented `imednet ...` CLI invocations against imednet.cli.get_parser()."""
    print("Checking documented imednet CLI commands against CLI argument parser...")
    default_root = Path(__file__).resolve().parent.parent
    for root in (repo_root, default_root):
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
    root_readme = repo_root / "README.md"
    if root_readme.is_file():
        candidates.append(root_readme)
    packages_dir = repo_root / "packages"
    if packages_dir.exists():
        candidates.extend(sorted(packages_dir.glob("*/README.md")))
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        candidates.extend(sorted(docs_dir.rglob("*.rst")))
        candidates.extend(sorted(docs_dir.rglob("*.md")))
    examples_dir = repo_root / "examples"
    if examples_dir.exists():
        candidates.extend(sorted(examples_dir.rglob("*.md")))

    has_errors = False
    checked_count = 0
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        rel_parts = path.relative_to(repo_root).parts
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
                        try:
                            with (
                                contextlib.redirect_stdout(io.StringIO()),
                                contextlib.redirect_stderr(io.StringIO()),
                            ):
                                parser.parse_args(cmd_tokens[1:])
                        except SystemExit as exc:
                            if exc.code not in (0, None):
                                print(
                                    f"Error: Invalid CLI command '{' '.join(cmd_tokens)}' at {path}:{lineno} "
                                    f"(exited with code {exc.code})"
                                )
                                has_errors = True
                        except _CLIValidationError as exc:
                            print(
                                f"Error: Invalid CLI command '{' '.join(cmd_tokens)}' at {path}:{lineno}: {exc}"
                            )
                            has_errors = True

    if not has_errors:
        print(f"CLI command validation passed ({checked_count} command(s) verified).")
    return has_errors


def check_packages_registered() -> bool:
    """Validate that all packages in /packages are registered across workspace and docs configs."""
    print("Checking if all packages are registered in build configuration...")

    packages_dir = Path("packages")
    if not packages_dir.exists():
        print(f"Warning: Path {packages_dir} not found.")
        return False

    conf_py_path = Path("docs/conf.py")
    index_rst_path = Path("docs/index.rst")
    plugins_rst_path = Path("docs/explanation/plugins.rst")
    cli_rst_path = Path("docs/how-to/cli.rst")
    pyproject_path = Path("pyproject.toml")
    readme_path = Path("README.md")

    if not conf_py_path.exists() or not index_rst_path.exists():
        print("Warning: docs/conf.py or docs/index.rst not found.")
        return False

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
        if "imednet[tui]" not in readme_content:
            print("Error: 'imednet[tui]' is missing from README.md installation instructions.")
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


def check_package_docs() -> bool:
    """Validate that all packages have README.md, CHANGELOG.md, and are mentioned in root CHANGELOG.md."""
    print(
        "Checking if all packages have local documentation and are mentioned in root CHANGELOG.md..."
    )
    packages_dir = Path("packages")
    if not packages_dir.exists():
        print(f"Warning: Path {packages_dir} not found.")
        return False

    root_changelog = Path("CHANGELOG.md")
    if not root_changelog.exists():
        print("Error: Root CHANGELOG.md not found.")
        return True

    root_changelog_content = root_changelog.read_text(encoding="utf-8")

    has_errors = False

    for pkg_dir in packages_dir.iterdir():
        if not pkg_dir.is_dir() or pkg_dir.name.startswith("."):
            continue

        pkg_name = pkg_dir.name

        # Check for local README.md
        readme_path = pkg_dir / "README.md"
        if not readme_path.exists():
            print(f"Error: Package {pkg_name} is missing a local README.md.")
            has_errors = True

        # Check for local CHANGELOG.md
        changelog_path = pkg_dir / "CHANGELOG.md"
        if not changelog_path.exists():
            print(f"Error: Package {pkg_name} is missing a local CHANGELOG.md.")
            has_errors = True

        # Check if package is mentioned in root CHANGELOG.md
        if pkg_name not in root_changelog_content:
            print(f"Error: Package {pkg_name} is not mentioned in the root CHANGELOG.md.")
            has_errors = True

    if not has_errors:
        print("All package docs checks passed.")
    return has_errors


def check_adr_records() -> bool:
    """Validate that all ADR records in docs/adr follow naming patterns and have valid titles."""
    print("Checking architecture decision records (ADRs)...")
    adr_dir = Path("docs/adr")
    if not adr_dir.exists():
        print(f"Warning: ADR directory {adr_dir} not found.")
        return False

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
        self.internal_hrefs: dict[str, list[str]] = {}
        self._dl_stack: list[_DlFrame] = []
        self._in_dt = False
        self._cur_dt_id: str | None = None
        self._cur_dt_chunks: list[str] = []

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
            href = attr_map.get("href")
            if "viewcode-back" in classes and href:
                self.backlinks.append(href)
            if "reference" in classes and "internal" in classes and href and self._dl_stack:
                for dt_id in self._dl_stack[-1].dt_ids:
                    self.internal_hrefs.setdefault(dt_id, []).append(href)

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
        if tag == "dt" and self._in_dt:
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

        for td_qname in (
            "imednet.orchestration.types.OrchestratorResult",
            "imednet_workflows.uat.inspector.FormVariableMap",
        ):
            if td_qname not in page_by_id:
                print(f"Error: Expected TypedDict '{td_qname}' missing from rendered API HTML.")
                has_errors = True
        if typeddict_classes < 2 or typeddict_keys < 6:
            print(
                f"Error: Expected >=2 TypedDict classes and >=6 keys, got {typeddict_classes} classes and {typeddict_keys} keys."
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

    target_ids_cache: dict[Path, set[str]] = {}
    broken_backlinks = 0
    total_backlinks = 0
    for mod_html in sorted((html_dir / "_modules").rglob("*.html")):
        if mod_html.name == "index.html":
            continue
        mod_parsed = _parse_html_file(mod_html)
        for href in mod_parsed.backlinks:
            total_backlinks += 1
            rel_part, _, frag = href.partition("#")
            target_file = (mod_html.parent / rel_part).resolve()
            if not target_file.exists():
                print(f"Error: Broken [docs] page link in {mod_html.relative_to(html_dir)}: {href}")
                broken_backlinks += 1
                has_errors = True
                continue
            if frag:
                if target_file not in target_ids_cache:
                    target_ids_cache[target_file] = _parse_html_file(target_file).ids
                if frag not in target_ids_cache[target_file]:
                    print(
                        f"Error: Broken [docs] fragment in {mod_html.relative_to(html_dir)}: {href}"
                    )
                    broken_backlinks += 1
                    has_errors = True

    if total_backlinks == 0:
        print("Error: Zero [docs] viewcode backlinks found in _modules/.")
        has_errors = True

    if not has_errors:
        print(
            f"Rendered API reference verified ({len(expected_models)} models, "
            f"{len(distinct_rendered_fields)} fields, {len(all_endpoints)} endpoints, "
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
    connected_events: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "templates_path"
                    and isinstance(node.value, ast.List)
                ):
                    templates_val = node.value.elts
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "templates_path"
            and isinstance(node.value, ast.List)
        ):
            templates_val = node.value.elts
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
    """Verify sitemap.xml, robots.txt, 404.html asset paths, and social metadata across built HTML."""
    target_dir = html_dir or Path("docs/_build/html")
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

    html_files = sorted(target_dir.rglob("*.html"))
    if not html_files:
        print(f"Error: No .html files found in {target_dir}.")
        has_errors = True

    required_meta_tokens = (
        '<meta name="description"',
        'property="og:title"',
        'property="og:description"',
        'property="og:url"',
        'property="og:image"',
        'name="twitter:card"',
    )
    for html_file in html_files:
        html_text = html_file.read_text(encoding="utf-8")
        for token in required_meta_tokens:
            if token not in html_text:
                rel = html_file.relative_to(target_dir)
                print(f"Error: {rel} is missing required metadata tag ({token}).")
                has_errors = True

    if not has_errors:
        print(
            f"Rendered HTML SEO check passed ({len(html_files)} HTML page(s), sitemap.xml, robots.txt, and 404.html verified)."
        )
    return has_errors


DEFAULT_HTML_DIR = Path("docs/_build/html")


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
    adr_failed = check_adr_records()
    githubpages_failed = check_sphinx_githubpages_extension()
    baseurl_failed = check_sphinx_html_baseurl()
    seo_config_failed = check_sphinx_seo_configuration()
    suppression_failed = check_no_duplicate_suppression()
    api_html_dir = args.html_dir if args.require_html or args.html_dir != DEFAULT_HTML_DIR else None
    api_html_failed = check_rendered_api_reference(api_html_dir)
    html_seo_failed = check_built_html_seo(args.html_dir, require_exists=args.require_html)

    if (
        todos_failed
        or extras_failed
        or cli_failed
        or packages_failed
        or docs_failed
        or adr_failed
        or githubpages_failed
        or baseurl_failed
        or seo_config_failed
        or suppression_failed
        or api_html_failed
        or html_seo_failed
    ):
        print("Validation failed.")
        sys.exit(1)

    print("Validation passed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
