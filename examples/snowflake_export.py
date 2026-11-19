"""Export Study Records to Snowflake using staged Parquet and MERGE INTO / COPY INTO.

Requires:
    pip install 'imednet[snowflake]'

Environment variables:
    IMEDNET_API_KEY       – iMednet API key
    IMEDNET_SECURITY_KEY  – iMednet security key (if required)
    IMEDNET_STUDY_KEY     – Target Study key (default: MY_STUDY)
    SNOWFLAKE_PASSWORD    – Snowflake user password (never hardcode)

Usage::

    python examples/snowflake_export.py
"""

from __future__ import annotations

import os

from imednet import ImednetSDK
from imednet_sinks import SnowflakeSinkConfig, export_to_snowflake

# ---------------------------------------------------------------------------
# SDK initialisation — credentials are read from environment variables only.
# ---------------------------------------------------------------------------
sdk = ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ.get("IMEDNET_SECURITY_KEY", ""),
)

STUDY_KEY = os.environ.get("IMEDNET_STUDY_KEY") or "MY_STUDY"

# ---------------------------------------------------------------------------
# Snowflake sink configuration.
# Credentials are passed via environment variables so they never appear in
# source code, logs, or exception messages.
# ---------------------------------------------------------------------------
config = SnowflakeSinkConfig(
    study_key=STUDY_KEY,
    account=os.environ.get("SNOWFLAKE_ACCOUNT") or "myorg-myaccount",
    user=os.environ.get("SNOWFLAKE_USER") or "loader",
    password=os.environ["SNOWFLAKE_PASSWORD"],
    database=os.environ.get("SNOWFLAKE_DATABASE") or "IMEDNET_DB",
    schema=os.environ.get("SNOWFLAKE_SCHEMA") or "PUBLIC",
    warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE") or "COMPUTE_WH",
    stage=os.environ.get("SNOWFLAKE_STAGE") or "MY_STAGE",
    table=os.environ.get("SNOWFLAKE_TABLE") or "RECORDS",
    # Optional: explicit local staging directory.  When omitted a temporary
    # directory is created and cleaned up automatically on exit.
    # local_staging_dir="/tmp/imednet_stage",
    #
    # Optional: JSONL manifest file for auditing loaded batches.
    # manifest_path="/tmp/imednet_manifest.jsonl",
    #
    # Idempotent mode (default True) stages Parquet batches and runs MERGE INTO
    # keyed on (study_key, record_id) so re-running updates Records in place.
    idempotent=True,
)

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
records_loaded = export_to_snowflake(sdk, STUDY_KEY, config=config)
print(f"Loaded {records_loaded} Records from Study '{STUDY_KEY}' into Snowflake.")
