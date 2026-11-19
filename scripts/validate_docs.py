"""Validation script for documentation."""

import argparse
import ast
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as DefusedET


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
        if "imednet-tui" not in root_deps:
            print(
                "Error: 'imednet-tui' is missing from root pyproject.toml [project].dependencies."
            )
            has_errors = True

    if readme_path.exists():
        readme_content = readme_path.read_text(encoding="utf-8")
        if "imednet[tui]" not in readme_content:
            print("Error: 'imednet[tui]' is missing from README.md installation instructions.")
            has_errors = True
        if "uv sync --all-packages --extra dev --extra docs" not in readme_content:
            print(
                "Error: 'uv sync --all-packages --extra dev --extra docs' is missing from README.md."
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for documentation validation."""
    parser = argparse.ArgumentParser(description="Validate documentation configuration and SEO.")
    parser.add_argument(
        "--html-dir",
        type=Path,
        default=Path("docs/_build/html"),
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
    packages_failed = check_packages_registered()
    docs_failed = check_package_docs()
    adr_failed = check_adr_records()
    githubpages_failed = check_sphinx_githubpages_extension()
    baseurl_failed = check_sphinx_html_baseurl()
    seo_config_failed = check_sphinx_seo_configuration()
    html_seo_failed = check_built_html_seo(args.html_dir, require_exists=args.require_html)

    if (
        todos_failed
        or packages_failed
        or docs_failed
        or adr_failed
        or githubpages_failed
        or baseurl_failed
        or seo_config_failed
        or html_seo_failed
    ):
        print("Validation failed.")
        sys.exit(1)

    print("Validation passed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
