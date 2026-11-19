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


def extract_acc_title(lines):
    """Extract non-empty accTitle value from Mermaid lines if present."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("accTitle:"):
            title = stripped.split(":", 1)[1].strip()
            if title:
                return title
    return None


def extract_nodes_from_mermaid_lines(lines):
    """Extract node ID -> label mappings while skipping accessibility directives."""
    nodes = {}
    in_acc_descr_block = False
    for line in lines:
        line_strip = line.strip()
        if in_acc_descr_block:
            if "}" in line_strip:
                in_acc_descr_block = False
            continue

        if line_strip.startswith("accTitle"):
            continue
        if line_strip.startswith("accDescr"):
            if "{" in line_strip and "}" not in line_strip:
                in_acc_descr_block = True
            continue

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
                    "flowchart ",
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
    return nodes


def collect_rst_mermaid_directives(docs_root):
    """Collect external and inline ``.. mermaid::`` directives across docs/**/*.rst."""
    external_refs = []
    inline_blocks = []
    directive_re = re.compile(r"^(\s*)\.\.\s+mermaid::(?:\s+(.*))?$")

    for root, dirs, files in os.walk(docs_root):
        dirs[:] = [d for d in dirs if d != "_build"]
        for file in sorted(files):
            if not file.endswith(".rst"):
                continue
            rst_path = os.path.join(root, file)
            rel_path = os.path.relpath(rst_path, docs_root)
            with open(rst_path, encoding="utf-8") as fh:
                raw_lines = fh.readlines()

            idx = 0
            while idx < len(raw_lines):
                match = directive_re.match(raw_lines[idx])
                if not match:
                    idx += 1
                    continue

                base_indent = len(match.group(1))
                arg = (match.group(2) or "").strip()
                start_line = idx + 1
                idx += 1

                block_lines = []
                while idx < len(raw_lines):
                    cur = raw_lines[idx]
                    stripped = cur.strip()
                    if not stripped:
                        block_lines.append(cur)
                        idx += 1
                        continue
                    cur_indent = len(cur) - len(cur.lstrip())
                    if cur_indent <= base_indent:
                        break
                    if not stripped.startswith(":"):
                        block_lines.append(cur)
                    idx += 1

                if arg:
                    external_refs.append((rel_path, start_line, arg))
                else:
                    content_lines = [ln for ln in block_lines if ln.strip()]
                    if content_lines:
                        inline_blocks.append((rel_path, start_line, content_lines))

    return external_refs, inline_blocks


def main():
    """Run the diagram validation."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docs_root = os.path.join(base_dir, "docs")
    docs_dir = os.path.join(docs_root, "diagrams")
    registry_path = os.path.join(docs_dir, "registry.json")

    with open(registry_path, encoding="utf-8") as f:
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

    for file in sorted(os.listdir(docs_dir)):
        if not file.endswith(".mmd"):
            continue

        filepath = os.path.join(docs_dir, file)
        with open(filepath, encoding="utf-8") as f:
            lines = f.readlines()

        if not extract_acc_title(lines):
            errors.append(f"File: {file} | Missing required accTitle accessibility directive")

        nodes = extract_nodes_from_mermaid_lines(lines)

        # Validate all unique labels
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

    external_refs, inline_blocks = collect_rst_mermaid_directives(docs_root)
    for rel_path, lineno, arg in external_refs:
        resolved = (
            os.path.join(docs_root, arg.lstrip("/"))
            if arg.startswith("/")
            else os.path.normpath(os.path.join(docs_root, os.path.dirname(rel_path), arg))
        )
        if not os.path.isfile(resolved):
            errors.append(f"RST: {rel_path}:{lineno} | Missing diagram file '{arg}'")
        else:
            with open(resolved, encoding="utf-8") as fh:
                if not extract_acc_title(fh.readlines()):
                    errors.append(
                        f"RST: {rel_path}:{lineno} | Referenced diagram '{arg}' missing accTitle"
                    )

    for rel_path, lineno, block_lines in inline_blocks:
        if not extract_acc_title(block_lines):
            errors.append(
                f"RST: {rel_path}:{lineno} | Inline mermaid diagram missing required accTitle"
            )

    total_rendered = len(external_refs) + len(inline_blocks)
    if total_rendered < 15:
        errors.append(
            f"Expected at least 15 rendered Mermaid diagrams across docs, found {total_rendered}"
        )

    if errors:
        print("Diagram validation failed:")
        for e in sorted(set(errors)):
            print(f" - {e}")
        sys.exit(1)

    print(f"All diagrams passed validation ({total_rendered} rendered diagrams verified)!")


if __name__ == "__main__":
    main()
