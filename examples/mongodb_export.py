"""Export Study Records to MongoDB.

Requires:
    pip install 'imednet[mongodb]'

Environment variables:
    IMEDNET_API_KEY       – iMednet API key
    IMEDNET_SECURITY_KEY  – iMednet security key (if required)
    IMEDNET_STUDY_KEY     – Target Study key (default: MY_STUDY)
    MONGODB_URI           – MongoDB connection URI (never hardcode credentials)
    MONGODB_DATABASE      – Target MongoDB database (default: imednet)
    MONGODB_COLLECTION    – Target MongoDB collection (default: records)

Usage::

    python examples/mongodb_export.py
"""

from __future__ import annotations

import os

from imednet import ImednetSDK
from imednet_sinks import MongoDbSinkConfig, export_to_mongodb

sdk = ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ.get("IMEDNET_SECURITY_KEY", ""),
)

STUDY_KEY = os.environ.get("IMEDNET_STUDY_KEY") or "MY_STUDY"
MONGODB_URI = os.environ.get("MONGODB_URI") or "mongodb://localhost:27017"
MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE") or "imednet"
MONGODB_COLLECTION = os.environ.get("MONGODB_COLLECTION") or "records"

config = MongoDbSinkConfig(
    study_key=STUDY_KEY,
    uri=MONGODB_URI,
    database=MONGODB_DATABASE,
    collection=MONGODB_COLLECTION,
    batch_size=500,
    idempotent=True,
)

records_loaded = export_to_mongodb(sdk, STUDY_KEY, config=config)
print(f"Loaded {records_loaded} Records from Study '{STUDY_KEY}' into MongoDB.")
