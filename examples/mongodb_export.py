"""Export study records to MongoDB.

Requires:
    pip install 'imednet[mongodb]'

Usage::

    python examples/mongodb_export.py
"""

from __future__ import annotations

import os

from imednet_sinks import MongoDbSinkConfig, export_to_mongodb

from imednet import ImednetSDK

sdk = ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ.get("IMEDNET_SECURITY_KEY", ""),
)

STUDY_KEY = os.environ.get("IMEDNET_STUDY_KEY", "MY_STUDY")
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "imednet")
MONGODB_COLLECTION = os.environ.get("MONGODB_COLLECTION", "records")

rows_loaded = export_to_mongodb(
    sdk,
    STUDY_KEY,
    config=MongoDbSinkConfig(
        study_key=STUDY_KEY,
        uri=MONGODB_URI,
        database=MONGODB_DATABASE,
        collection=MONGODB_COLLECTION,
        batch_size=500,
        idempotent=True,
    ),
)
print(f"Loaded {rows_loaded} rows from study '{STUDY_KEY}' into MongoDB.")
