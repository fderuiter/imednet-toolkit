"""Export records to a normalized long-format SQL table.

Set ``IMEDNET_API_KEY`` and ``IMEDNET_SECURITY_KEY`` before running this script
(or enable ``IMEDNET_SIMULATION_MODE=true``). Optionally set ``IMEDNET_BASE_URL``
for non-default instances.

Usage:
    uv run --with 'imednet[export]' python examples/export_long_sql.py STUDY_KEY TABLE_NAME OUTPUT_DB

The ``OUTPUT_DB`` path is used to build the SQLite connection string.
"""

from __future__ import annotations

import os
import sys
import tempfile

from imednet import ImednetSDK
from imednet.integrations import export_to_long_sql


def main() -> None:
    """Export a study's records in long format to SQLite."""
    sim_mode = os.getenv("IMEDNET_SIMULATION_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if len(sys.argv) == 1 and sim_mode:
        study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"
        table_name = "records_long"
        with tempfile.TemporaryDirectory() as tmpdir, ImednetSDK() as sdk:
            output_db = os.path.join(tmpdir, "records_long.db")
            export_to_long_sql(sdk, study_key, table_name, f"sqlite:///{output_db}")
            print(f"Exported {study_key} to {output_db} using table '{table_name}'.")
        return

    if len(sys.argv) != 4:
        print(
            'Usage: uv run --with "imednet[export]" python examples/export_long_sql.py STUDY_KEY TABLE_NAME OUTPUT_DB',
            file=sys.stderr,
        )
        sys.exit(1)

    study_key, table_name, output_db = sys.argv[1:]

    if not sim_mode:
        missing = [var for var in ("IMEDNET_API_KEY", "IMEDNET_SECURITY_KEY") if not os.getenv(var)]
        if missing:
            vars_ = ", ".join(missing)
            print(f"Missing required environment variable(s): {vars_}", file=sys.stderr)
            sys.exit(1)

    with ImednetSDK() as sdk:
        conn_str = f"sqlite:///{output_db}"
        export_to_long_sql(sdk, study_key, table_name, conn_str)
    print(f"Exported {study_key} to {output_db} using table '{table_name}'.")


if __name__ == "__main__":
    main()
