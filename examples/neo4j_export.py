"""Export study records to Neo4j.

Requires:
    pip install 'imednet[neo4j]'

Environment variables:
    NEO4J_PASSWORD  – Neo4j password (never hardcode)

Usage::

    python examples/neo4j_export.py
"""

from __future__ import annotations

import os

from imednet_sinks import Neo4jSinkConfig, export_to_neo4j

from imednet import ImednetSDK

sdk = ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ.get("IMEDNET_SECURITY_KEY", ""),
)

STUDY_KEY = os.environ.get("IMEDNET_STUDY_KEY", "MY_STUDY")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")

rows_loaded = export_to_neo4j(
    sdk,
    STUDY_KEY,
    config=Neo4jSinkConfig(
        study_key=STUDY_KEY,
        uri=NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        database=NEO4J_DATABASE,
        batch_size=500,
        idempotent=True,
    ),
)
print(f"Loaded {rows_loaded} rows from study '{STUDY_KEY}' into Neo4j.")
