"""Export study records to Snowflake using staged Parquet + COPY INTO.

Requires:
    pip install 'imednet[snowflake]'

Environment variables:
    IMEDNET_API_KEY       – iMednet API key
    IMEDNET_SECURITY_KEY  – iMednet security key (if required)
    SNOWFLAKE_PASSWORD    – Snowflake user password (never hardcode)

Usage::

    python examples/snowflake_export.py
"""

from __future__ import annotations

import os

from imednet_sinks import SnowflakeSinkConfig, export_to_snowflake

from imednet import ImednetSDK

# ---------------------------------------------------------------------------
# SDK initialisation — credentials are read from environment variables only.
# ---------------------------------------------------------------------------
sdk = ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ.get("IMEDNET_SECURITY_KEY", ""),
)

STUDY_KEY = os.environ.get("IMEDNET_STUDY_KEY", "MY_STUDY")

# ---------------------------------------------------------------------------
# Snowflake sink configuration.
# Credentials are passed via environment variables so they never appear in
# source code, logs, or exception messages.
# ---------------------------------------------------------------------------
config = SnowflakeSinkConfig(
    study_key=STUDY_KEY,
    account=os.environ.get("SNOWFLAKE_ACCOUNT", "myorg-myaccount"),
    user=os.environ.get("SNOWFLAKE_USER", "loader"),
    password=os.environ["SNOWFLAKE_PASSWORD"],
    database=os.environ.get("SNOWFLAKE_DATABASE", "IMEDNET_DB"),
    schema=os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
    warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
    stage=os.environ.get("SNOWFLAKE_STAGE", "MY_STAGE"),
    table=os.environ.get("SNOWFLAKE_TABLE", "RECORDS"),
    # Optional: explicit local staging directory.  When omitted a temporary
    # directory is created and cleaned up automatically on exit.
    # local_staging_dir="/tmp/imednet_stage",
    #
    # Optional: JSONL manifest file for auditing loaded batches.
    # manifest_path="/tmp/imednet_manifest.jsonl",
    #
    # Idempotent mode (default True) uses FORCE = FALSE in COPY INTO so that
    # re-running this script does not reload already-ingested files.
    idempotent=True,
)

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
rows_loaded = export_to_snowflake(sdk, STUDY_KEY, config=config)
print(f"Loaded {rows_loaded} rows from study '{STUDY_KEY}' into Snowflake.")
