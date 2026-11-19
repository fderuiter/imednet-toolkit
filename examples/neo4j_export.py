"""Export Study Records to Neo4j.

Requires:
    pip install 'imednet[neo4j]'

Environment variables:
    IMEDNET_API_KEY       – iMednet API key
    IMEDNET_SECURITY_KEY  – iMednet security key (if required)
    IMEDNET_STUDY_KEY     – Target Study key (default: MY_STUDY)
    NEO4J_URI             – Neo4j Bolt/routing URI (default: bolt://localhost:7687)
    NEO4J_USER            – Neo4j username (default: neo4j)
    NEO4J_PASSWORD        – Neo4j password (never hardcode)
    NEO4J_DATABASE        – Target Neo4j database (default: neo4j)

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

STUDY_KEY = os.environ.get("IMEDNET_STUDY_KEY") or "MY_STUDY"
NEO4J_URI = os.environ.get("NEO4J_URI") or "bolt://localhost:7687"
NEO4J_USER = os.environ.get("NEO4J_USER") or "neo4j"
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE") or "neo4j"

config = Neo4jSinkConfig(
    study_key=STUDY_KEY,
    uri=NEO4J_URI,
    auth=(NEO4J_USER, NEO4J_PASSWORD),
    database=NEO4J_DATABASE,
    batch_size=500,
    idempotent=True,
)

records_loaded = export_to_neo4j(sdk, STUDY_KEY, config=config)
print(f"Loaded {records_loaded} Records from Study '{STUDY_KEY}' into Neo4j.")
