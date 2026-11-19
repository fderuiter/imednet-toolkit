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
        """Map Postman placeholder strings and primitive examples to internal type names."""
        if isinstance(val, bool):
            return "boolean", False
        if isinstance(val, int):
            return "integer", 0
        if isinstance(val, str):
            if val == "<string>":
                return "string", None
            if val == "<integer>":
                return "integer", 0
            if val == "<boolean>":
                return "boolean", False
            return "string", None
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

    def ingest_openapi(self, file_path: str) -> None:
        """Ingest an OpenAPI specification JSON file."""
        if not os.path.exists(file_path):
            return

        with open(file_path) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return

        components = data.get("components", {})
        schemas = components.get("schemas", {})

        for model_name, schema in schemas.items():
            if schema.get("type") == "object":
                properties = schema.get("properties", {})
                fields = {}
                for key, prop in properties.items():
                    snake_key = to_snake(key)

                    type_name = "any"
                    default_value = None
                    prop_type = prop.get("type")
                    if prop_type == "string":
                        type_name = "string"
                        default_value = ""
                    elif prop_type == "integer":
                        type_name = "integer"
                        default_value = 0  # type: ignore
                    elif prop_type == "boolean":
                        type_name = "boolean"
                        default_value = False  # type: ignore

                    fields[snake_key] = FieldDefinition(
                        name=snake_key,
                        type_name=type_name,
                        default_value=default_value,
                        alias=key,
                    )

                self.contract.models[model_name] = ModelDefinition(name=model_name, fields=fields)

        openapi_paths = data.get("paths", {})
        for path, path_obj in openapi_paths.items():
            for method, method_obj in path_obj.items():
                if method.lower() != "get":
                    continue
                responses = method_obj.get("responses", {})
                for status, response_obj in responses.items():
                    if status.startswith("2"):
                        content = response_obj.get("content", {})
                        for content_type, content_obj in content.items():  # noqa: B007
                            schema = content_obj.get("schema", {})
                            ref = None

                            if "$ref" in schema:
                                ref = schema["$ref"]
                            elif (
                                schema.get("type") == "array"
                                and "items" in schema
                                and "$ref" in schema["items"]
                            ):
                                ref = schema["items"]["$ref"]
                            elif "properties" in schema:
                                data_prop = schema["properties"].get("data")
                                if data_prop and "$ref" in data_prop:
                                    ref = data_prop["$ref"]
                                elif (
                                    data_prop
                                    and data_prop.get("type") == "array"
                                    and "items" in data_prop
                                    and "$ref" in data_prop["items"]
                                ):
                                    ref = data_prop["items"]["$ref"]
                                elif "recordData" in schema["properties"]:
                                    data_prop = schema["properties"]["recordData"]
                                    if data_prop and "$ref" in data_prop:
                                        ref = data_prop["$ref"]
                                    elif (
                                        data_prop
                                        and data_prop.get("type") == "array"
                                        and "items" in data_prop
                                        and "$ref" in data_prop["items"]
                                    ):
                                        ref = data_prop["items"]["$ref"]

                            if ref:
                                model_name = ref.split("/")[-1]
                                path_segments = [p for p in path.split("/") if p]
                                for p in reversed(path_segments):
                                    if (  # noqa: SIM102
                                        isinstance(p, str)
                                        and not p.startswith('{')
                                        and not p.startswith(':')
                                    ):
                                        if p.isalpha() or p.replace('_', '').isalpha():
                                            self.contract.paths[p] = model_name
                                            break
