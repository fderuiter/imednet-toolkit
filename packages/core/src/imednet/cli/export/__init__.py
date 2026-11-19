"""CLI commands for exporting data."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

from ...models.records import Record
from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, fetching_status, print_error
from ..utils.args import add_list_query_args, build_list_query_kwargs


def _build_export_call_kwargs(
    *,
    filters: list[str] | None,
    record_data_filter: str | None,
    sort: str | None,
) -> dict[str, Any]:
    """Build optional export keyword arguments only when supplied on the CLI."""
    raw = build_list_query_kwargs(
        filters=filters,
        sort=sort,
        record_data_filter=record_data_filter,
        model=Record,
    )
    export_kwargs: dict[str, Any] = {}
    rdf = raw.pop("record_data_filter", None)
    sort_val = raw.pop("sort", None)
    if raw:
        export_kwargs["filters"] = raw
    if rdf is not None:
        export_kwargs["record_data_filter"] = rdf
    if sort_val is not None:
        export_kwargs["sort"] = sort_val
    return export_kwargs


def _require_sinks_plugin(sdk: ImednetSDK, sink_name: str) -> Any:
    """Ensure ``imednet-plugins-sinks`` is installed and loaded on ``sdk.sinks``."""
    if sdk.sinks is None:
        print_error(
            f"Error: imednet-plugins-sinks is required for {sink_name} export. "
            'Install with "pip install imednet-plugins-sinks".'
        )
        sys.exit(1)
    return sdk.sinks


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("export", help="Export study data to various formats.")
    sub = parser.add_subparsers(dest="command")

    # parquet
    pq_parser = sub.add_parser("parquet", help="Export study records to a Parquet file.")
    pq_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    pq_parser.add_argument("path", type=Path, help="Destination Parquet file.")
    pq_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of names as column headers.",
    )
    add_list_query_args(
        pq_parser,
        include_record_data_filter=True,
        filter_example="form_key=DEMO",
    )

    @with_sdk
    def export_parquet(
        sdk: ImednetSDK,
        study_key: str,
        path: Path,
        use_labels: bool = False,
        filters: list[str] | None = None,
        record_data_filter: str | None = None,
        sort: str | None = None,
    ) -> None:
        if importlib.util.find_spec("pyarrow") is None:
            print_error(
                "Error: pyarrow is required for Parquet export. Install with \"pip install 'imednet[export]'\"."
            )
            sys.exit(1)
        query_kwargs = _build_export_call_kwargs(
            filters=filters,
            record_data_filter=record_data_filter,
            sort=sort,
        )
        from .. import export_to_parquet

        with fetching_status("records for Parquet export", study_key):
            export_to_parquet(
                sdk,
                study_key,
                str(path),
                use_labels_as_columns=use_labels,
                **query_kwargs,
            )

    pq_parser.set_defaults(
        func=lambda args: export_parquet(
            study_key=args.study_key,
            path=args.path,
            use_labels=args.use_labels,
            filters=args.filters,
            record_data_filter=args.record_data_filter,
            sort=args.sort,
        )
    )

    # csv
    csv_parser = sub.add_parser("csv", help="Export study records to a CSV file.")
    csv_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    csv_parser.add_argument("path", type=Path, help="Destination CSV file.")
    csv_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of names as column headers.",
    )
    add_list_query_args(
        csv_parser,
        include_record_data_filter=True,
        filter_example="form_key=DEMO",
    )

    @with_sdk
    def export_csv(
        sdk: ImednetSDK,
        study_key: str,
        path: Path,
        use_labels: bool = False,
        filters: list[str] | None = None,
        record_data_filter: str | None = None,
        sort: str | None = None,
    ) -> None:
        if importlib.util.find_spec("pandas") is None:
            print_error("Error: pandas is required for CSV export.")
            sys.exit(1)
        query_kwargs = _build_export_call_kwargs(
            filters=filters,
            record_data_filter=record_data_filter,
            sort=sort,
        )
        from .. import export_to_csv

        with fetching_status("records for CSV export", study_key):
            export_to_csv(
                sdk,
                study_key,
                str(path),
                use_labels_as_columns=use_labels,
                **query_kwargs,
            )

    csv_parser.set_defaults(
        func=lambda args: export_csv(
            study_key=args.study_key,
            path=args.path,
            use_labels=args.use_labels,
            filters=args.filters,
            record_data_filter=args.record_data_filter,
            sort=args.sort,
        )
    )

    # excel
    xl_parser = sub.add_parser("excel", help="Export study records to an Excel workbook.")
    xl_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    xl_parser.add_argument("path", type=Path, help="Destination Excel workbook.")
    xl_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of names as column headers.",
    )
    add_list_query_args(
        xl_parser,
        include_record_data_filter=True,
        filter_example="form_key=DEMO",
    )

    @with_sdk
    def export_excel(
        sdk: ImednetSDK,
        study_key: str,
        path: Path,
        use_labels: bool = False,
        filters: list[str] | None = None,
        record_data_filter: str | None = None,
        sort: str | None = None,
    ) -> None:
        if (
            importlib.util.find_spec("pandas") is None
            or importlib.util.find_spec("openpyxl") is None
        ):
            print_error("Error: pandas and openpyxl are required for Excel export.")
            sys.exit(1)
        query_kwargs = _build_export_call_kwargs(
            filters=filters,
            record_data_filter=record_data_filter,
            sort=sort,
        )
        from .. import export_to_excel

        with fetching_status("records for Excel export", study_key):
            export_to_excel(
                sdk,
                study_key,
                str(path),
                use_labels_as_columns=use_labels,
                **query_kwargs,
            )

    xl_parser.set_defaults(
        func=lambda args: export_excel(
            study_key=args.study_key,
            path=args.path,
            use_labels=args.use_labels,
            filters=args.filters,
            record_data_filter=args.record_data_filter,
            sort=args.sort,
        )
    )

    # json
    json_parser = sub.add_parser("json", help="Export study records to a JSON file.")
    json_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    json_parser.add_argument("path", type=Path, help="Destination JSON file.")
    json_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of names as column headers.",
    )
    json_parser.add_argument(
        "--hierarchical",
        action="store_true",
        help="Export nested hierarchical tree structure.",
    )
    add_list_query_args(
        json_parser,
        include_record_data_filter=True,
        filter_example="form_key=DEMO",
    )

    @with_sdk
    def export_json_cmd(
        sdk: ImednetSDK,
        study_key: str,
        path: Path,
        use_labels: bool = False,
        hierarchical: bool = False,
        filters: list[str] | None = None,
        record_data_filter: str | None = None,
        sort: str | None = None,
    ) -> None:
        query_kwargs = _build_export_call_kwargs(
            filters=filters,
            record_data_filter=record_data_filter,
            sort=sort,
        )
        from .. import export_to_json

        with fetching_status("records for JSON export", study_key):
            export_to_json(
                sdk,
                study_key,
                str(path),
                use_labels_as_columns=use_labels,
                hierarchical=hierarchical,
                **query_kwargs,
            )

    json_parser.set_defaults(
        func=lambda args: export_json_cmd(
            study_key=args.study_key,
            path=args.path,
            use_labels=args.use_labels,
            hierarchical=args.hierarchical,
            filters=args.filters,
            record_data_filter=args.record_data_filter,
            sort=args.sort,
        )
    )

    # duckdb
    ddb_parser = sub.add_parser("duckdb", help="Export study records to a DuckDB table.")
    ddb_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    ddb_parser.add_argument("table_name", help="Destination DuckDB table name.")
    ddb_parser.add_argument("db_path", type=Path, help="Path to DuckDB database file.")
    ddb_parser.add_argument(
        "--vars", dest="vars_", help="Comma-separated list of variable names to include."
    )
    ddb_parser.add_argument("--forms", help="Comma-separated list of form IDs to include.")
    ddb_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of names as column headers.",
    )

    @with_sdk
    def export_duckdb(
        sdk: ImednetSDK,
        study_key: str,
        table_name: str,
        db_path: Path,
        vars_: str | None = None,
        forms: str | None = None,
        use_labels: bool = False,
    ) -> None:
        if importlib.util.find_spec("duckdb") is None:
            print_error(
                "Error: duckdb is required for DuckDB export. Install with \"pip install 'imednet[duckdb]'\""
            )
            sys.exit(1)
        from ...integrations.export import _validate_duckdb_identifier

        try:
            _validate_duckdb_identifier(table_name)
        except ValueError as exc:
            print_error(f"Error: {exc}")
            sys.exit(1)

        from .. import export_to_duckdb

        var_list = [v.strip() for v in vars_.split(",")] if vars_ else None
        form_list = [int(f.strip()) for f in forms.split(",")] if forms else None
        with fetching_status("records for DuckDB export", study_key):
            export_to_duckdb(
                sdk,
                study_key,
                str(db_path),
                table_name,
                use_labels_as_columns=use_labels,
                variable_whitelist=var_list,
                form_whitelist=form_list,
            )

    ddb_parser.set_defaults(
        func=lambda args: export_duckdb(
            study_key=args.study_key,
            table_name=args.table_name,
            db_path=args.db_path,
            vars_=args.vars_,
            forms=args.forms,
            use_labels=args.use_labels,
        )
    )

    # sql
    sql_parser = sub.add_parser("sql", help="Export study records to a SQL table.")
    sql_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    sql_parser.add_argument("table", help="Destination table name.")
    sql_parser.add_argument("connection_string", help="Database connection string.")
    sql_parser.add_argument(
        "--single-table",
        action="store_true",
        help="Store all records in a single table even when using SQLite.",
    )
    sql_parser.add_argument(
        "--long-format", action="store_true", help="Export normalized long-format table."
    )
    sql_parser.add_argument(
        "--vars", dest="vars_", help="Comma-separated list of variable names to include."
    )
    sql_parser.add_argument("--forms", help="Comma-separated list of form IDs to include.")

    @with_sdk
    def export_sql(
        sdk: ImednetSDK,
        study_key: str,
        table: str,
        connection_string: str,
        single_table: bool = False,
        long_format: bool = False,
        vars_: str | None = None,
        forms: str | None = None,
    ) -> None:
        if importlib.util.find_spec("sqlalchemy") is None:
            print_error("Error: SQLAlchemy is required for SQL export.")
            sys.exit(1)
        from sqlalchemy import create_engine

        from .. import export_to_long_sql, export_to_sql, export_to_sql_by_form

        engine = create_engine(connection_string)
        var_list = [v.strip() for v in vars_.split(",")] if vars_ else None
        form_list = [int(f.strip()) for f in forms.split(",")] if forms else None
        with fetching_status("records for SQL export", study_key):
            if long_format:
                export_to_long_sql(sdk, study_key, table, connection_string)
                return
            if not single_table and engine.dialect.name == "sqlite":
                export_to_sql_by_form(
                    sdk,
                    study_key,
                    connection_string,
                    variable_whitelist=var_list,
                    form_whitelist=form_list,
                )
            else:
                export_to_sql(
                    sdk,
                    study_key,
                    table,
                    connection_string,
                    variable_whitelist=var_list,
                    form_whitelist=form_list,
                )

    sql_parser.set_defaults(
        func=lambda args: export_sql(
            study_key=args.study_key,
            table=args.table,
            connection_string=args.connection_string,
            single_table=args.single_table,
            long_format=args.long_format,
            vars_=args.vars_,
            forms=args.forms,
        )
    )

    # mongodb
    m_parser = sub.add_parser("mongodb", help="Export study records to MongoDB document envelopes.")
    m_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    m_parser.add_argument("uri", help="MongoDB connection URI.")
    m_parser.add_argument("database", help="MongoDB database name.")
    m_parser.add_argument("collection", help="MongoDB collection name.")
    m_parser.add_argument("--batch-size", type=int, default=500, help="Records per batch.")
    m_parser.add_argument(
        "--insert-only",
        action="store_false",
        dest="upsert",
        help="Use insert-only mode instead of idempotent upserts.",
    )

    @with_sdk
    def export_mongodb(
        sdk: ImednetSDK,
        study_key: str,
        uri: str,
        database: str,
        collection: str,
        batch_size: int = 500,
        upsert: bool = True,
    ) -> None:
        if importlib.util.find_spec("pymongo") is None:
            print_error(
                "Error: pymongo is required for MongoDB export. Install with \"pip install 'imednet[mongodb]'\""
            )
            sys.exit(1)
        sinks = _require_sinks_plugin(sdk, "MongoDB")
        cfg = sinks.MongoDbSinkConfig(
            study_key=study_key,
            uri=uri,
            database=database,
            collection=collection,
            batch_size=batch_size,
            idempotent=upsert,
        )
        with fetching_status("records for MongoDB export", study_key):
            sinks.export_to_mongodb(sdk, study_key, config=cfg)

    m_parser.set_defaults(
        func=lambda args: export_mongodb(
            study_key=args.study_key,
            uri=args.uri,
            database=args.database,
            collection=args.collection,
            batch_size=args.batch_size,
            upsert=args.upsert,
        )
    )

    # neo4j
    n_parser = sub.add_parser(
        "neo4j", help="Export study records to Neo4j nodes and relationships."
    )
    n_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    n_parser.add_argument("uri", help="Neo4j URI.")
    n_parser.add_argument("username", help="Neo4j username.")
    n_parser.add_argument("password", help="Neo4j password.")
    n_parser.add_argument("--database", default="neo4j", help="Neo4j database name.")
    n_parser.add_argument("--batch-size", type=int, default=500, help="Records per batch.")
    n_parser.add_argument(
        "--create-only",
        action="store_false",
        dest="merge",
        help="Use CREATE-only writes instead of idempotent MERGE.",
    )

    @with_sdk
    def export_neo4j(
        sdk: ImednetSDK,
        study_key: str,
        uri: str,
        username: str,
        password: str,
        database: str = "neo4j",
        batch_size: int = 500,
        merge: bool = True,
    ) -> None:
        if importlib.util.find_spec("neo4j") is None:
            print_error(
                "Error: neo4j is required for Neo4j export. Install with \"pip install 'imednet[neo4j]'\""
            )
            sys.exit(1)
        sinks = _require_sinks_plugin(sdk, "Neo4j")
        cfg = sinks.Neo4jSinkConfig(
            study_key=study_key,
            uri=uri,
            auth=(username, password),
            batch_size=batch_size,
            idempotent=merge,
            database=database,
        )
        with fetching_status("records for Neo4j export", study_key):
            sinks.export_to_neo4j(sdk, study_key, config=cfg)

    n_parser.set_defaults(
        func=lambda args: export_neo4j(
            study_key=args.study_key,
            uri=args.uri,
            username=args.username,
            password=args.password,
            database=args.database,
            batch_size=args.batch_size,
            merge=args.merge,
        )
    )

    # snowflake
    s_parser = sub.add_parser(
        "snowflake", help="Export study records to Snowflake using staged Parquet + COPY INTO."
    )
    s_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    s_parser.add_argument("account", help="Snowflake account identifier.")
    s_parser.add_argument("user", help="Snowflake username.")
    s_parser.add_argument("password", help="Snowflake password.")
    s_parser.add_argument("database", help="Target Snowflake database.")
    s_parser.add_argument("schema", help="Target Snowflake schema.")
    s_parser.add_argument("warehouse", help="Snowflake warehouse.")
    s_parser.add_argument("stage", help="Internal stage name.")
    s_parser.add_argument("table", help="Target table name.")
    s_parser.add_argument("--stage-prefix", default="imednet", help="Path prefix in stage.")
    s_parser.add_argument(
        "--local-staging-dir",
        type=Path,
        default=None,
        help="Local directory for staged Parquet files.",
    )
    s_parser.add_argument(
        "--manifest-path", type=Path, default=None, help="Optional JSONL manifest output path."
    )
    s_parser.add_argument("--batch-size", type=int, default=500, help="Records per batch.")
    s_parser.add_argument(
        "--force-reload",
        action="store_false",
        dest="idempotent",
        help="Force reloading already-loaded files.",
    )

    @with_sdk
    def export_snowflake(
        sdk: ImednetSDK,
        study_key: str,
        account: str,
        user: str,
        password: str,
        database: str,
        schema: str,
        warehouse: str,
        stage: str,
        table: str,
        stage_prefix: str = "imednet",
        local_staging_dir: Path | None = None,
        manifest_path: Path | None = None,
        batch_size: int = 500,
        idempotent: bool = True,
    ) -> None:
        if (
            importlib.util.find_spec("snowflake.connector") is None
            or importlib.util.find_spec("pyarrow") is None
        ):
            print_error(
                "Error: snowflake-connector-python and pyarrow are required for Snowflake export. Install with \"pip install 'imednet[snowflake]'\""
            )
            sys.exit(1)
        sinks = _require_sinks_plugin(sdk, "Snowflake")
        cfg = sinks.SnowflakeSinkConfig(
            study_key=study_key,
            account=account,
            user=user,
            password=password,
            database=database,
            schema=schema,
            warehouse=warehouse,
            stage=stage,
            table=table,
            stage_prefix=stage_prefix,
            local_staging_dir=local_staging_dir,
            manifest_path=manifest_path,
            batch_size=batch_size,
            idempotent=idempotent,
        )
        with fetching_status("records for Snowflake export", study_key):
            sinks.export_to_snowflake(sdk, study_key, config=cfg)

    s_parser.set_defaults(
        func=lambda args: export_snowflake(
            study_key=args.study_key,
            account=args.account,
            user=args.user,
            password=args.password,
            database=args.database,
            schema=args.schema,
            warehouse=args.warehouse,
            stage=args.stage,
            table=args.table,
            stage_prefix=args.stage_prefix,
            local_staging_dir=args.local_staging_dir,
            manifest_path=args.manifest_path,
            batch_size=args.batch_size,
            idempotent=args.idempotent,
        )
    )
