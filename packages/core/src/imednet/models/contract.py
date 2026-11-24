"""Unified contract definitions."""

import json
import os
import re
from typing import Any

from pydantic import BaseModel, Field


class FieldDefinition(BaseModel):
    """Internal contract for a model field."""

    name: str
    type_name: str
    default_value: Any = None
    alias: str | None = None


class ModelDefinition(BaseModel):
    """Internal contract for a model."""

    name: str
    fields: dict[str, FieldDefinition]


class APIContract(BaseModel):
    """Unified API Contract."""

    models: dict[str, ModelDefinition] = Field(default_factory=dict)
    paths: dict[str, str] = Field(default_factory=dict)


def to_snake(name: str) -> str:
    """Convert camelCase or PascalCase strings to snake_case."""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


class ContractBuilder:
    """Builder for APIContract."""

    def __init__(self) -> None:
        """Initialize."""
        self.contract = APIContract()

    def parse_postman_type(self, val: Any) -> tuple[str, Any]:
        """Map Postman placeholder strings and literal JSON values to internal type names and defaults."""
        if isinstance(val, bool):
            return "boolean", None
        if isinstance(val, int):
            return "integer", None
        if isinstance(val, float):
            return "float", None
        if isinstance(val, str):
            if val == "<boolean>":
                return "boolean", None
            if val == "<integer>":
                return "integer", None
            if val in {"<float>", "<double>", "<number>"}:
                return "float", None
            return "string", None
        if isinstance(val, dict):
            return "dict", None
        if isinstance(val, list):
            return "list", None
        return "any", None

    def ingest_postman(self, file_path: str) -> None:
        """Ingest a Postman collection JSON file."""
        if not os.path.exists(file_path):
            return

        with open(file_path) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return

        name_mapping = {
            "Study information": "Study",
            "List of forms": "Form",
            "Variable list": "Variable",
            "Interval list": "Interval",
            "Site list": "Site",
            "Subject list": "Subject",
            "Record list": "Record",
            "Job created": "Job",
            "Job status": "JobStatus",
            "Record revision list": "RecordRevision",
            "Coding list": "Coding",
            "Query list": "Query",
            "Visit list": "Visit",
            "User list": "User",
        }

        def extract_schemas(items: list[dict[str, Any]]) -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                if 'item' in item:
                    extract_schemas(item['item'])
                elif 'response' in item:
                    if not isinstance(item['response'], list):
                        continue
                    for resp in item['response']:
                        if not isinstance(resp, dict):
                            continue
                        if resp.get('name') in name_mapping and 'body' in resp:
                            body = resp['body']
                            if body and isinstance(body, str):
                                try:
                                    parsed = json.loads(body)
                                    if not isinstance(parsed, dict):
                                        continue
                                    model_name = name_mapping[resp['name']]
                                    schema_dict = None
                                    if (
                                        'data' in parsed
                                        and isinstance(parsed['data'], list)
                                        and len(parsed['data']) > 0
                                    ):
                                        schema_dict = parsed['data'][0]
                                    elif 'data' not in parsed and 'metadata' not in parsed:
                                        schema_dict = parsed

                                    if schema_dict:
                                        fields = {}
                                        for key, val in schema_dict.items():
                                            snake_key = to_snake(key)
                                            type_name, default_value = self.parse_postman_type(val)
                                            fields[snake_key] = FieldDefinition(
                                                name=snake_key,
                                                type_name=type_name,
                                                default_value=default_value,
                                                alias=key,
                                            )
                                        self.contract.models[model_name] = ModelDefinition(
                                            name=model_name, fields=fields
                                        )

                                        # Map path to model dynamically
                                        req = resp.get('originalRequest', {})
                                        method = req.get('method', 'GET')
                                        if method.upper() == 'GET':
                                            url = req.get('url', {})
                                            path_segments = url.get('path', [])
                                            if path_segments:
                                                # Typically paths are like ['studies'] or ['api', 'v1', 'studies']
                                                # We want to map the base resource name to the model.
                                                for p in reversed(path_segments):
                                                    if (  # noqa: SIM102
                                                        isinstance(p, str)
                                                        and not p.startswith(':')
                                                        and not p.startswith('{{')
                                                    ):
                                                        # Keep only alphabetic base resource names to avoid mapping UUIDs or weird paths
                                                        if (
                                                            p.isalpha()
                                                            or p.replace('_', '').isalpha()
                                                        ):
                                                            self.contract.paths[p] = model_name
                                                            break

                                except Exception:  # noqa: S110
                                    pass

        extract_schemas(data.get('item', []))

    @staticmethod
    def _unwrap_nullable_type(raw_type: Any) -> str | None:
        """Normalize an OpenAPI `type` string or nullable type array (e.g., `['number', 'null']`)."""
        if isinstance(raw_type, str):
            return None if raw_type == "null" else raw_type
        if isinstance(raw_type, list):
            non_null = [t for t in raw_type if isinstance(t, str) and t != "null"]
            if len(non_null) == 1:
                return non_null[0]
        return None

    @classmethod
    def _resolve_openapi_schema(
        cls,
        schema: Any,
        root: dict[str, Any],
        seen: set[str] | None = None,
    ) -> dict[str, Any]:
        """Dereference local `$ref` pointers and single-branch `allOf`/`anyOf`/`oneOf` wrappers."""
        if not isinstance(schema, dict):
            return {}
        visited = set() if seen is None else set(seen)

        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") and ref not in visited:
            visited.add(ref)
            target: Any = root
            for part in ref[2:].split("/"):
                if isinstance(target, dict) and part in target:
                    target = target[part]
                else:
                    target = None
                    break
            if isinstance(target, dict):
                resolved_target = cls._resolve_openapi_schema(target, root, visited)
                merged = dict(resolved_target)
                for k, v in schema.items():
                    if k != "$ref":
                        merged[k] = v
                return merged

        for combiner in ("allOf", "anyOf", "oneOf"):
            branches = schema.get(combiner)
            if isinstance(branches, list):
                non_null_branches = [
                    b for b in branches if isinstance(b, dict) and b.get("type") != "null"
                ]
                if len(non_null_branches) == 1:
                    resolved_branch = cls._resolve_openapi_schema(
                        non_null_branches[0], root, visited
                    )
                    merged = dict(resolved_branch)
                    for k, v in schema.items():
                        if k != combiner:
                            merged[k] = v
                    return merged

        return schema

    @classmethod
    def _extract_schema_ref(cls, schema: Any) -> str | None:
        """Extract component `$ref` from direct, array, nullable-union, or envelope schemas."""
        if not isinstance(schema, dict):
            return None
        ref_val = schema.get("$ref")
        if isinstance(ref_val, str):
            return ref_val

        for combiner in ("allOf", "anyOf", "oneOf"):
            branches = schema.get(combiner)
            if isinstance(branches, list):
                for branch in branches:
                    found = cls._extract_schema_ref(branch)
                    if found:
                        return found

        schema_type = cls._unwrap_nullable_type(schema.get("type"))
        if schema_type == "array" and isinstance(schema.get("items"), dict):
            found = cls._extract_schema_ref(schema["items"])
            if found:
                return found

        props = schema.get("properties")
        if isinstance(props, dict):
            for envelope_key in ("data", "recordData"):
                if envelope_key in props:
                    found = cls._extract_schema_ref(props[envelope_key])
                    if found:
                        return found
        return None

    def _infer_openapi_property_type(self, prop: Any, root: dict[str, Any]) -> str:
        """Map an OpenAPI property schema (including `$ref` and nullable arrays) to internal type."""
        if not isinstance(prop, dict):
            return "any"
        resolved = self._resolve_openapi_schema(prop, root)
        prop_type = self._unwrap_nullable_type(resolved.get("type"))
        if prop_type == "string":
            return "string"
        if prop_type == "integer":
            return "integer"
        if prop_type in {"number", "float"}:
            return "float"
        if prop_type == "boolean":
            return "boolean"
        if prop_type == "array":
            return "list"
        if (
            prop_type == "object"
            or isinstance(resolved.get("properties"), dict)
            or "additionalProperties" in resolved
        ):
            return "dict"

        for candidate_schema in (prop, resolved):
            for literal_key in ("example", "default", "const"):
                if literal_key in candidate_schema and candidate_schema[literal_key] is not None:
                    type_name, _ = self.parse_postman_type(candidate_schema[literal_key])
                    return type_name
            enum_vals = candidate_schema.get("enum")
            if isinstance(enum_vals, list) and enum_vals:
                non_null_enum = [v for v in enum_vals if v is not None]
                if non_null_enum:
                    type_name, _ = self.parse_postman_type(non_null_enum[0])
                    return type_name
        return "any"

    def ingest_openapi(self, file_path: str) -> None:
        """Ingest an OpenAPI specification JSON file."""
        if not os.path.exists(file_path):
            return

        with open(file_path) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return

        components = data.get("components", {})
        schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
        if not isinstance(schemas, dict):
            schemas = {}

        for model_name, raw_schema in schemas.items():
            schema = self._resolve_openapi_schema(raw_schema, data)
            schema_type = self._unwrap_nullable_type(schema.get("type"))
            if schema_type == "object" or isinstance(schema.get("properties"), dict):
                properties = schema.get("properties", {})
                if not isinstance(properties, dict):
                    continue
                fields = {}
                for key, prop in properties.items():
                    snake_key = to_snake(key)
                    type_name = self._infer_openapi_property_type(prop, data)
                    fields[snake_key] = FieldDefinition(
                        name=snake_key,
                        type_name=type_name,
                        default_value=None,
                        alias=key,
                    )

                self.contract.models[model_name] = ModelDefinition(name=model_name, fields=fields)

        openapi_paths = data.get("paths", {})
        if not isinstance(openapi_paths, dict):
            return

        for path, path_obj in openapi_paths.items():
            if not isinstance(path_obj, dict):
                continue
            for method, method_obj in path_obj.items():
                method_lower = method.lower()
                if method_lower not in {"get", "post"} or not isinstance(method_obj, dict):
                    continue

                ref: str | None = None
                responses = method_obj.get("responses", {})
                if isinstance(responses, dict):
                    for status, response_obj in responses.items():
                        if not str(status).startswith("2") or not isinstance(response_obj, dict):
                            continue
                        resolved_resp = self._resolve_openapi_schema(response_obj, data)
                        content = resolved_resp.get("content", {})
                        if not isinstance(content, dict):
                            continue
                        for content_obj in content.values():
                            if isinstance(content_obj, dict):
                                ref = self._extract_schema_ref(content_obj.get("schema", {}))
                                if ref:
                                    break
                        if ref:
                            break

                if ref is None and method_lower == "post":
                    req_body = self._resolve_openapi_schema(method_obj.get("requestBody", {}), data)
                    req_content = req_body.get("content", {})
                    if isinstance(req_content, dict):
                        for content_obj in req_content.values():
                            if isinstance(content_obj, dict):
                                ref = self._extract_schema_ref(content_obj.get("schema", {}))
                                if ref:
                                    break

                if ref:
                    model_name = ref.split("/")[-1]
                    path_segments = [p for p in path.split("/") if p]
                    for p in reversed(path_segments):
                        if (
                            isinstance(p, str)
                            and not p.startswith("{")
                            and not p.startswith(":")
                            and (p.isalpha() or p.replace("_", "").isalpha())
                        ):
                            if p not in self.contract.paths or method_lower == "get":
                                self.contract.paths[p] = model_name
                            break
