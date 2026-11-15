"""Validation script for documentation."""

import ast
import re
import sys
from pathlib import Path


def check_todos_in_init() -> bool:
    """Ensure no TODO strings exist in operator __init__ docstrings."""
    print("Checking for TODOs in __init__ docstrings...")
    base_path = Path("packages/providers-airflow/src/apache_airflow_providers_imednet")
    if not base_path.exists():
        print(f"Warning: Path {base_path} not found.")
        return False

    has_todos = False
    for py_file in base_path.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(content, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "__init__":
                        docstring = ast.get_docstring(child)
                        if docstring and "TODO" in docstring:
                            print(
                                f"Error: TODO found in __init__ docstring of class {node.name} in {py_file}"
                            )
                            has_todos = True
    if not has_todos:
        print("No TODOs found in __init__ docstrings.")
    return has_todos


def check_packages_registered() -> bool:
    """Validate that all packages in /packages are registered in docs/conf.py and Makefile."""
    print("Checking if all packages are registered in build configuration...")

    packages_dir = Path("packages")
    if not packages_dir.exists():
        print(f"Warning: Path {packages_dir} not found.")
        return False

    conf_py_path = Path("docs/conf.py")
    index_rst_path = Path("docs/index.rst")

    if not conf_py_path.exists() or not index_rst_path.exists():
        print("Warning: docs/conf.py or docs/index.rst not found.")
        return False

    conf_py_content = conf_py_path.read_text(encoding="utf-8")
    index_rst_content = index_rst_path.read_text(encoding="utf-8")

    has_errors = False

    for pkg_dir in packages_dir.iterdir():
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
    conf_py = Path("docs/conf.py")
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


def main():
    """Run validation scripts."""
    todos_failed = check_todos_in_init()
    packages_failed = check_packages_registered()
    docs_failed = check_package_docs()
    adr_failed = check_adr_records()
    githubpages_failed = check_sphinx_githubpages_extension()

    if todos_failed or packages_failed or docs_failed or adr_failed or githubpages_failed:
        print("Validation failed.")
        sys.exit(1)

    print("Validation passed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
