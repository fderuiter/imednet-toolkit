"""Automated extraction and generation of REST reference tables from Postman collection."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

RESOURCE_METADATA: dict[str, dict[str, str]] = {
    "studies": {
        "title": "Studies",
        "description": "Clinical study protocols, site configuration metadata, and trial registry records.",
    },
    "forms": {
        "title": "Forms",
        "description": "Case Report Form (CRF) templates, versioned revisions, and clinical layout specifications.",
    },
    "variables": {
        "title": "Variables",
        "description": "Clinical study variables, CRF question definitions, data types, and codelist references.",
    },
    "intervals": {
        "title": "Intervals",
        "description": "Study visit schedule intervals, timeline phases, and window calculation criteria.",
    },
    "sites": {
        "title": "Sites",
        "description": "Clinical research trial sites, investigator locations, and participant enrollment statuses.",
    },
    "subjects": {
        "title": "Subjects",
        "description": "Enrolled clinical study subjects, screening participant entries, and study status records.",
    },
    "records": {
        "title": "Records",
        "description": "Clinical data records, form instances, and transactional record creation endpoints.",
    },
    "record_revisions": {
        "title": "Record Revisions",
        "description": "Comprehensive 21 CFR Part 11 audit trail revisions, change reasons, and edit histories.",
    },
    "codings": {
        "title": "Codings",
        "description": "Medical dictionary coding assignments (MedDRA / WHODrug) for adverse events and treatments.",
    },
    "queries": {
        "title": "Queries",
        "description": "Clinical data discrepancy queries, resolution comments, and clarification requests.",
    },
    "visits": {
        "title": "Visits",
        "description": "Subject visit event instances, milestone completion dates, and window compliance records.",
    },
    "users": {
        "title": "Users",
        "description": "User accounts, role assignments, and investigator study access permissions.",
    },
    "jobs": {
        "title": "Jobs",
        "description": "Asynchronous batch processing status and progress monitoring for background operations.",
    },
}

QUERY_PARAM_DESCRIPTIONS: dict[str, str] = {
    "page": "Zero-based page index for paged results.",
    "size": "Maximum number of records to return per page.",
    "sort": "Sorting expression (e.g. ``studyKey,asc`` or ``dateCreated,desc``).",
    "filter": "Attribute query filter expression (see :ref:`filter_syntax`).",
    "recordDataFilter": "Dynamic question data filter expression (see :ref:`filter_syntax`).",
    "includeInactive": "Whether to include inactive users in the returned result set.",
}

PATH_VAR_DESCRIPTIONS: dict[str, str] = {
    "studyKey": "Unique key identifying the clinical study protocol.",
    "batchId": "Unique batch tracking identifier for asynchronous record creation job.",
}

BODY_FIELD_DESCRIPTIONS: dict[str, tuple[str, bool, str]] = {
    "formKey": ("String", True, "Clinical Case Report Form (CRF) unique key identifier."),
    "subjectKey": ("String", True, "Target study participant unique key identifier."),
    "siteName": ("String", True, "Name of the clinical research site where data was collected."),
    "intervalName": ("String", True, "Study visit or interval event name."),
    "data": (
        "Object",
        True,
        "Clinical data dictionary mapping item/question variable names to recorded values.",
    ),
}


def _normalize_resource_name(folder_name: str) -> str:
    """Map Postman folder names to canonical clinical resource keys."""
    cleaned = folder_name.strip("/").split("/")[-1]
    mapping = {
        "recordRevisions": "record_revisions",
        "records": "records",
        "studies": "studies",
        "forms": "forms",
        "variables": "variables",
        "intervals": "intervals",
        "sites": "sites",
        "subjects": "subjects",
        "codings": "codings",
        "queries": "queries",
        "visits": "visits",
        "users": "users",
        "jobs": "jobs",
        "{batchId}": "jobs",
        "{studyKey}": "studies",
    }
    return mapping.get(cleaned, cleaned)


def _extract_operations(
    items: list[dict[str, Any]], current_folder: str = ""
) -> list[tuple[str, dict[str, Any]]]:
    """Recursively traverse collection items and associate operations with their resource folder."""
    operations: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        if "item" in item:
            name = item.get("name", "")
            next_folder = f"{current_folder}/{name}" if current_folder else name
            operations.extend(_extract_operations(item["item"], next_folder))
        elif "request" in item:
            operations.append((current_folder, item))
    return operations


def _format_route_path(url_obj: dict[str, Any]) -> str:
    """Format the API route path with /api/v1/edc prefix and {variable} syntax."""
    path_segments = url_obj.get("path", [])
    formatted_segments: list[str] = []
    for seg in path_segments:
        if seg.startswith(":"):
            formatted_segments.append(f"{{{seg[1:]}}}")
        else:
            formatted_segments.append(seg)
    path_str = "/" + "/".join(formatted_segments)
    if not path_str.startswith("/api/v1/edc"):
        path_str = f"/api/v1/edc{path_str}"
    return path_str


def _generate_endpoint_rst(operation: dict[str, Any]) -> list[str]:
    """Generate reStructuredText for a single API endpoint operation."""
    op_name = operation.get("name", "Endpoint")
    req = operation.get("request", {})
    method = req.get("method", "GET").upper()
    url_obj = req.get("url", {})
    route_path = _format_route_path(url_obj)

    lines: list[str] = [
        f"{op_name}",
        "^" * len(op_name),
        "",
        ".. rubric:: HTTP Route",
        "",
        f"``{method} {route_path}``",
        "",
    ]

    # HTTP Headers table
    lines.extend(
        [
            ".. rubric:: Request Headers",
            "",
            ".. list-table::",
            "   :widths: 25 15 15 45",
            "   :header-rows: 1",
            "",
            "   * - Header",
            "     - Type",
            "     - Required",
            "     - Description",
            "   * - ``x-api-key``",
            "     - String",
            "     - Required",
            "     - API key credential for client authentication.",
            "   * - ``x-imn-security-key``",
            "     - String",
            "     - Required",
            "     - User security key token for request authorization.",
            "   * - ``Accept``",
            "     - String",
            "     - Required",
            "     - Response media type. Must be ``application/json``.",
        ]
    )
    if method == "POST" or req.get("body"):
        lines.extend(
            [
                "   * - ``Content-Type``",
                "     - String",
                "     - Required",
                "     - Request media type. Must be ``application/json``.",
            ]
        )
    lines.append("")

    # Path variables
    path_vars = url_obj.get("variable", [])
    if path_vars:
        lines.extend(
            [
                ".. rubric:: Path Variables",
                "",
                ".. list-table::",
                "   :widths: 25 15 15 45",
                "   :header-rows: 1",
                "",
                "   * - Variable",
                "     - Type",
                "     - Required",
                "     - Description",
            ]
        )
        for var in path_vars:
            key = var.get("key", "")
            desc = PATH_VAR_DESCRIPTIONS.get(
                key, f"Target {key} parameter for clinical resource identification."
            )
            lines.extend(
                [
                    f"   * - ``{key}``",
                    "     - String",
                    "     - Required",
                    f"     - {desc}",
                ]
            )
        lines.append("")

    # Query parameters
    query_params = url_obj.get("query", [])
    if query_params:
        lines.extend(
            [
                ".. rubric:: Query Parameters",
                "",
                ".. list-table::",
                "   :widths: 25 15 15 45",
                "   :header-rows: 1",
                "",
                "   * - Parameter",
                "     - Type",
                "     - Default",
                "     - Description",
            ]
        )
        for q in query_params:
            key = q.get("key", "")
            raw_val = q.get("value", "")
            default_val = f"``{raw_val}``" if raw_val and raw_val != "<string>" else "None"
            type_str = (
                "Integer"
                if key in ("page", "size")
                else ("Boolean" if key == "includeInactive" else "String")
            )
            desc = QUERY_PARAM_DESCRIPTIONS.get(key, f"Optional filter attribute for {key}.")
            lines.extend(
                [
                    f"   * - ``{key}``",
                    f"     - {type_str}",
                    f"     - {default_val}",
                    f"     - {desc}",
                ]
            )
        lines.append("")

    # Request Body (if any)
    body = req.get("body", {})
    body_raw = body.get("raw")
    if body_raw:
        lines.extend(
            [
                ".. rubric:: Request Body Schema",
                "",
                ".. list-table::",
                "   :widths: 25 15 15 45",
                "   :header-rows: 1",
                "",
                "   * - Field",
                "     - Type",
                "     - Required",
                "     - Description",
            ]
        )
        for field, (f_type, f_req, f_desc) in BODY_FIELD_DESCRIPTIONS.items():
            req_str = "Required" if f_req else "Optional"
            lines.extend(
                [
                    f"   * - ``{field}``",
                    f"     - {f_type}",
                    f"     - {req_str}",
                    f"     - {f_desc}",
                ]
            )
        lines.extend(
            [
                "",
                ".. rubric:: Sample Request Payload",
                "",
                ".. code-block:: json",
                "",
            ]
        )
        for b_line in body_raw.splitlines():
            lines.append(f"   {b_line}")
        lines.append("")

    # Response status codes & payload
    responses = operation.get("response", [])
    primary_code = 200
    primary_status = "OK"
    sample_response_body: str | None = None

    if responses:
        first_resp = responses[0]
        primary_code = first_resp.get("code", 200)
        primary_status = first_resp.get("status", "OK")
        resp_body = first_resp.get("body")
        if resp_body:
            sample_response_body = resp_body

    lines.extend(
        [
            ".. rubric:: Status Codes",
            "",
            ".. list-table::",
            "   :widths: 15 25 60",
            "   :header-rows: 1",
            "",
            "   * - Status",
            "     - Reason",
            "     - Description",
            f"   * - ``{primary_code}``",
            f"     - {primary_status}",
            f"     - Successful response: {op_name.lower()}.",
            "   * - ``400``",
            "     - Bad Request",
            "     - Invalid query parameters, missing required fields, or malformed JSON payload.",
            "   * - ``401``",
            "     - Unauthorized",
            "     - Missing or invalid ``x-api-key`` or ``x-imn-security-key`` headers.",
            "   * - ``403``",
            "     - Forbidden",
            "     - Insufficient permissions to access clinical study resources or invalid studyKey.",
            "   * - ``404``",
            "     - Not Found",
            "     - Resource, study key, or record identifier not found.",
            "   * - ``429``",
            "     - Too Many Requests",
            "     - API rate limit threshold exceeded for current credentials.",
            "   * - ``500``",
            "     - Server Error",
            "     - Internal server error occurred during request processing.",
            "",
        ]
    )

    if sample_response_body:
        lines.extend(
            [
                ".. rubric:: Sample Response Payload",
                "",
                ".. code-block:: json",
                "",
            ]
        )
        for r_line in sample_response_body.splitlines():
            lines.append(f"   {r_line}")
        lines.append("")

    return lines


def generate_rest_reference(
    collection_path: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> list[Path]:
    """Parse Postman collection and generate structured reStructuredText reference files."""
    repo_root = Path(__file__).resolve().parent.parent

    if collection_path is None:
        env_path = os.environ.get("IMEDNET_POSTMAN_PATH")
        if env_path:
            collection_path = Path(env_path)
        else:
            collection_path = repo_root / "imednet.postman_collection.json"
    else:
        collection_path = Path(collection_path)

    if not collection_path.exists():
        raise FileNotFoundError(f"Postman collection not found at: {collection_path}")

    if output_dir is None:
        output_dir = repo_root / "docs" / "reference" / "rest_tables"
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    with open(collection_path, encoding="utf-8") as f:
        collection_data = json.load(f)

    raw_operations = _extract_operations(collection_data.get("item", []))

    # Group operations by resource
    resource_ops: dict[str, list[dict[str, Any]]] = {res_key: [] for res_key in RESOURCE_METADATA}

    for folder, op in raw_operations:
        res_key = _normalize_resource_name(folder)
        if res_key in resource_ops:
            resource_ops[res_key].append(op)
        else:
            # Fallback based on URL path
            url_path = op.get("request", {}).get("url", {}).get("path", [])
            matched = False
            for seg in reversed(url_path):
                norm = _normalize_resource_name(seg)
                if norm in resource_ops:
                    resource_ops[norm].append(op)
                    matched = True
                    break
            if not matched:
                resource_ops["studies"].append(op)

    generated_files: list[Path] = []

    for res_key, meta in RESOURCE_METADATA.items():
        ops = resource_ops.get(res_key, [])
        file_path = output_dir / f"{res_key}.rst"

        title = meta["title"]
        desc = meta["description"]
        rst_lines: list[str] = [
            f".. _rest_resource_{res_key}:",
            "",
            f"{title}",
            "~" * len(title),
            "",
            f"{desc}",
            "",
        ]

        if not ops:
            rst_lines.extend(
                [
                    "No endpoint operations currently documented for this clinical resource.",
                    "",
                ]
            )
        else:
            for op in ops:
                rst_lines.extend(_generate_endpoint_rst(op))

        content = "\n".join(rst_lines).rstrip() + "\n"
        file_path.write_text(content, encoding="utf-8")
        generated_files.append(file_path)

    print(
        f"Generated REST reference tables for {len(generated_files)} clinical resources in {output_dir}"
    )
    return generated_files


def main() -> None:
    """CLI entrypoint for standalone reference generation."""
    try:
        generate_rest_reference()
    except Exception as e:
        print(f"Error generating REST reference documentation: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
