"""Script to validate mermaid diagram references."""

import ast
import json
import os
import re
import sys


def get_code_symbols(search_dirs):
    """Extract code symbols from given directories."""
    symbols = set()
    for directory in search_dirs:
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(".py"):
                    with open(os.path.join(root, file), encoding="utf-8") as f:
                        try:
                            tree = ast.parse(f.read())
                            for node in ast.walk(tree):
                                if isinstance(node, ast.ClassDef):
                                    symbols.add(node.name)
                                    for child in node.body:
                                        if isinstance(child, ast.FunctionDef):
                                            symbols.add(child.name)
                                            symbols.add(f"{node.name}.{child.name}")
                                elif isinstance(node, ast.FunctionDef):
                                    symbols.add(node.name)
                        except Exception:
                            pass
    return symbols


def get_facade_signatures():
    """Return explicit facade signatures."""
    return {
        "subjects.list",
        "subjects.get",
        "records.list",
        "records.create",
        "records.get",
        "variables.list",
        "queries.list",
        "visits.list",
        "sdk.variables.list",
    }


def clean_label(label):
    """Clean the label by removing parentheses."""
    c = re.sub(r'\(.*?\)', '', label).strip()
    return c.replace('?', '').strip()


def main():
    """Run the diagram validation."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docs_dir = os.path.join(base_dir, "docs", "diagrams")
    registry_path = os.path.join(docs_dir, "registry.json")

    with open(registry_path) as f:
        registry = json.load(f) or {}

    search_dirs = [
        os.path.join(base_dir, "packages", "core", "src", "imednet"),
        os.path.join(base_dir, "packages", "plugins-workflows", "src", "imednet_workflows"),
        os.path.join(
            base_dir, "packages", "providers-airflow", "src", "apache_airflow_providers_imednet"
        ),
        os.path.join(base_dir, "packages", "plugins-sinks", "src"),
    ]
    code_symbols = get_code_symbols(search_dirs)
    facade_sigs = get_facade_signatures()
    all_symbols = code_symbols | facade_sigs

    errors = []

    for file in os.listdir(docs_dir):
        if not file.endswith(".mmd"):
            continue

        filepath = os.path.join(docs_dir, file)

        # Pass 1: Extract all explicit labels
        # node_id -> label
        nodes = {}
        with open(filepath) as f:
            for line in f:
                line_strip = line.strip()
                if line_strip.startswith("participant"):
                    m = re.match(r'participant\s+(\w+)\s+as\s+"?(.*?)"?$', line_strip)
                    if m:
                        nodes[m.group(1)] = m.group(2)
                    else:
                        m2 = re.match(r'participant\s+(\w+)', line_strip)
                        if m2:
                            nodes[m2.group(1)] = m2.group(1)
                    continue

                if (
                    line_strip.startswith(
                        (
                            "%%",
                            "graph ",
                            "subgraph ",
                            "Note ",
                            "activate ",
                            "deactivate ",
                            "sequenceDiagram",
                        )
                    )
                    or line_strip == "end"
                ):
                    continue

                parts = re.split(r'-->>|->>|--!?>|---|==>|<--!?>|<--|-.->', line_strip)
                if len(parts) == 1 and not re.search(r'\[|\(', line_strip):
                    # Probably not a node definition
                    continue

                for part in parts:
                    part_strip = part.strip()
                    if not part_strip:
                        continue
                    # remove edge labels
                    cleaned_part = re.sub(r'^\|[^|]+\|\s*', '', part_strip).strip()

                    m = re.search(
                        r'^([a-zA-Z0-9_.-]+)(?:\["([^"]*)"\]|\[([^\]]*)\]|\("([^"]*)"\)|\(([^)]*)\)|\{"([^"]*)"\}|\{([^}]*)\})?',
                        cleaned_part,
                    )
                    if m:
                        id_ = m.group(1)
                        label = next((g for g in m.groups()[1:] if g is not None), None)
                        if label:
                            nodes[id_] = label
                        elif id_ not in nodes:
                            nodes[id_] = id_

        # Now validate all unique labels
        for id_, label in nodes.items():
            if label in registry:
                continue
            if id_ in registry:
                continue

            cleaned = clean_label(label)
            if cleaned in all_symbols or label in all_symbols or id_ in all_symbols:
                continue

            # Allow strings that match specific internal names if not in registry
            errors.append(f"File: {file} | Node ID: {id_} | Unknown Label: '{label}'")

    if errors:
        print("Diagram validation failed:")
        for e in sorted(set(errors)):
            print(f" - {e}")
        sys.exit(1)

    print("All diagrams passed validation!")


if __name__ == "__main__":
    main()
