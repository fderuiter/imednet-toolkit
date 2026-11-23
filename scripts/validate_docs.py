"""Validation script for documentation."""

import argparse
import ast
import contextlib
import importlib
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as DefusedET

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
    for root in (root_dir, REPO_ROOT):
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


def check_package_docs(repo_root: Path | None = None) -> bool:
    """Validate package README/CHANGELOG files, root CHANGELOG index hygiene, and MyST changelog hub."""
    print(
        "Checking if all packages have local documentation, clean root CHANGELOG.md, and MyST includes..."
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

    if "imednet-python-sdk" in root_changelog_content:
        print("Error: Root CHANGELOG.md contains legacy 'imednet-python-sdk' links.")
        has_errors = True
    if "imednet.compat.v1" in root_changelog_content:
        print("Error: Root CHANGELOG.md contains stale 'imednet.compat.v1' deprecation claims.")
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

        changelog_path = pkg_dir / "CHANGELOG.md"
        if not changelog_path.is_file():
            print(f"Error: Package {pkg_name} is missing a local CHANGELOG.md.")
            has_errors = True

        if f"packages/{pkg_name}/CHANGELOG.md" not in root_changelog_content:
            print(
                f"Error: Package changelog 'packages/{pkg_name}/CHANGELOG.md' is not linked in root CHANGELOG.md."
            )
            has_errors = True

    if not has_errors:
        print("All package docs and MyST changelog checks passed.")
    return has_errors


def check_release_please_parity(repo_root: Path | None = None) -> bool:
    """Verify release-please-config.json extra-files and version parity with .release-please-manifest.json."""
    print("Checking Release Please extra-files and manifest version parity...")
    root = repo_root or REPO_ROOT
    config_path = root / "release-please-config.json"
    manifest_path = root / ".release-please-manifest.json"
    core_init_path = root / "packages" / "core" / "src" / "imednet" / "__init__.py"
    thebe_js_path = root / "docs" / "_static" / "thebe_pyodide.js"

    for req_path in (config_path, manifest_path, core_init_path, thebe_js_path):
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
    for pkg_key, expected_ver in sorted(manifest_data.items()):
        if pkg_key not in packages_cfg:
            print(
                f"Error: '{pkg_key}' in .release-please-manifest.json is missing from release-please-config.json."
            )
            has_errors = True
            continue
        pkg_entry = packages_cfg[pkg_key]
        if isinstance(pkg_entry, dict):
            for ef in pkg_entry.get("extra-files", []):
                norm = _normalize_extra_path(pkg_key, ef)
                if norm:
                    declared_extra_paths.add(norm)

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

    for required_extra in (
        "packages/core/src/imednet/__init__.py",
        "docs/_static/thebe_pyodide.js",
    ):
        if required_extra not in declared_extra_paths:
            print(
                f"Error: release-please-config.json is missing extra-files entry for '{required_extra}'."
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
    if "x-release-please-version" not in thebe_text:
        print("Error: docs/_static/thebe_pyodide.js is missing 'x-release-please-version' marker.")
        has_errors = True

    wheel_manifest_map = (
        ("imednet", "packages/core"),
        ("imednet_workflows", "packages/plugins-workflows"),
        ("imednet_plugins_sinks", "packages/plugins-sinks"),
    )
    for wheel_prefix, manifest_key in wheel_manifest_map:
        expected_wheel_ver = str(manifest_data.get(manifest_key, ""))
        wheel_match = re.search(
            rf"_static/wheels/{re.escape(wheel_prefix)}-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl",
            thebe_text,
        )
        if not wheel_match:
            print(
                f"Error: Fallback wheel entry for '{wheel_prefix}' not found in docs/_static/thebe_pyodide.js."
            )
            has_errors = True
        elif wheel_match.group(1) != expected_wheel_ver:
            print(
                f"Error: docs/_static/thebe_pyodide.js wheel '{wheel_prefix}' version "
                f"'{wheel_match.group(1)}' != manifest '{expected_wheel_ver}'."
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


class _ApiHtmlExtractor(HTMLParser):
    """Lightweight stdlib HTML parser collecting IDs, dl blocks, dt text, and viewcode backlinks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.dt_text: dict[str, str] = {}
        self.dl_text_by_dt_id: dict[str, str] = {}
        self.backlinks: list[str] = []
        self._dl_stack: list[tuple[list[str], list[str]]] = []
        self._cur_dt_id: str | None = None
        self._cur_dt_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        elem_id = attr_map.get("id")
        if elem_id:
            self.ids.add(elem_id)

        if tag == "dl":
            self._dl_stack.append(([], []))
        elif tag == "dt":
            self._cur_dt_id = elem_id
            self._cur_dt_chunks = []
            if elem_id and self._dl_stack:
                self._dl_stack[-1][0].append(elem_id)
        elif tag == "a":
            classes = (attr_map.get("class") or "").split()
            href = attr_map.get("href")
            if "viewcode-back" in classes and href:
                self.backlinks.append(href)

    def handle_data(self, data: str) -> None:
        if self._cur_dt_id is not None:
            self._cur_dt_chunks.append(data)
        for _dt_ids, chunks in self._dl_stack:
            chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "dt" and self._cur_dt_id is not None:
            self.dt_text[self._cur_dt_id] = " ".join("".join(self._cur_dt_chunks).split())
            self._cur_dt_id = None
            self._cur_dt_chunks = []
        elif tag == "dl" and self._dl_stack:
            dt_ids, chunks = self._dl_stack.pop()
            text = " ".join("".join(chunks).split())
            for dt_id in dt_ids:
                self.dl_text_by_dt_id.setdefault(dt_id, text)


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


def check_rendered_api_reference(html_dir: Path | None = None) -> bool:
    """Verify rendered HTML model fields, endpoint methods, _modules/index.html, and [docs] backlinks."""
    print("Checking rendered Sphinx HTML API reference and viewcode backlinks...")
    repo_root = Path(__file__).resolve().parents[1]
    core_src = str(repo_root / "packages" / "core" / "src")
    if core_src not in sys.path:
        sys.path.insert(0, core_src)

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

    has_errors = False

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
    release_please_failed = check_release_please_parity()
    examples_failed = check_examples_index_parity()
    sidebars_failed = check_diataxis_sidebar_isolation()
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
        or release_please_failed
        or examples_failed
        or sidebars_failed
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
