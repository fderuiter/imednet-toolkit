"""CLI commands for exporting data."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

from ...endpoints.records import RecordsEndpoint
from ...integrations.sink_base import normalize_watermark_datetime
from ...models.records import Record
from ...sdk import ImednetSDK
from ..decorators import validate_cli_path_segment, with_sdk
from ..utils import (
    add_no_interactive,
    add_simulation_arg,
    add_study_key_args,
    fetching_status,
    parse_filter_args,
    parse_record_data_filter,
    print_error,
    resolve_study_key,
    simulation_mode_context,
)


def _add_export_study_args(parser: argparse.ArgumentParser) -> None:
    """Attach hybrid study key and simulation flags to an export subcommand."""
    add_study_key_args(parser)
    add_simulation_arg(parser)


def _resolve_export_study(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """Resolve and validate study_key for an export subcommand."""
    study_key = resolve_study_key(parser, args, required=True)
    assert study_key is not None
    validate_cli_path_segment(study_key, name="study_key")
    return study_key


def _add_streaming_export_args(parser: argparse.ArgumentParser) -> None:
    """Attach unified watermark, filtering, and checkpoint flags to an export subcommand."""
    parser.add_argument(
        "--since",
        dest="since",
        default=None,
        help="Export records modified on or after this ISO-8601 timestamp or date.",
    )
    parser.add_argument(
        "--filter",
        dest="filters",
        action="append",
        default=None,
        help=(
            "Server filter expression using =, ==, !=, <, <=, >, >= and ';' (AND) "
            "or ',' (OR) connectors (e.g. 'subjectKey==SUBJ-01'). Repeatable."
        ),
    )
    parser.add_argument(
        "--record-data-filter",
        dest="record_data_filter",
        default=None,
        help=(
            "Clinical variable recordDataFilter expression using ==, !=, <, <=, >, >=, =~ "
            "(e.g. 'AESER==Serious;AGE>=18' or 'AETERM=~^Head')."
        ),
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        default=False,
        help="Read and commit incremental high-water mark checkpoints via state ledger.",
    )
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        default=False,
        help="Include voided (deleted=True) records in export output.",
    )
    parser.add_argument(
        "--stream-name",
        dest="stream_name",
        default=None,
        help="Optional checkpoint stream identifier.",
    )
    parser.add_argument(
        "--ledger-path",
        dest="ledger_path",
        type=Path,
        default=None,
        help="Optional file path for file-backed checkpoint state ledger.",
    )
    parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action="store_true",
        default=True,
        help="Mask sensitive Subject PHI fields with ***MASKED*** (enabled by default).",
    )
    parser.add_argument(
        "--no-mask-phi",
        dest="mask_phi",
        action="store_false",
        help="Disable default PHI masking for authorized raw exports.",
    )
    parser.add_argument(
        "--quality-gate",
        dest="quality_gate_enabled",
        action="store_true",
        default=None,
        help="Enable pre-write record schema readiness quality gate filtering.",
    )
    parser.add_argument(
        "--no-quality-gate",
        dest="quality_gate_enabled",
        action="store_false",
        help="Disable pre-write record schema readiness quality gate filtering.",
    )
    parser.add_argument(
        "--min-readiness-score",
        dest="min_schema_readiness_score",
        type=float,
        default=None,
        help="Minimum record schema readiness score (0-100) when quality gate is enabled.",
    )
    parser.add_argument(
        "--as-sdtm",
        dest="as_sdtm",
        action="store_true",
        default=False,
        help="Transform exported study records into CDISC SDTM domain format.",
    )
    parser.add_argument(
        "--sdtm-domain",
        "--domain",
        dest="sdtm_domain",
        default=None,
        help="Target CDISC SDTM domain code (e.g. AE, DM, VS, CM, DV, DD); implies --as-sdtm.",
    )
    from ...models.standards import PROFILE_REGISTRY

    parser.add_argument(
        "--standards-profile",
        dest="standards_profile",
        choices=PROFILE_REGISTRY.list_profiles(),
        default=None,
        help="CDISC standards readiness profile used to validate study batches.",
    )
    parser.add_argument(
        "--min-standards-score",
        dest="min_standards_score",
        type=float,
        default=None,
        help="Minimum CDISC StandardsReadinessValidator score (0-100) required per batch.",
    )
    parser.add_argument(
        "--naming-convention",
        dest="naming_convention",
        choices=["snake_case", "camelCase", "sdtm"],
        default=None,
        help="Variable and metadata key naming convention when --as-sdtm is off.",
    )
    add_no_interactive(parser)


def _parse_export_filters(filters: list[str] | None) -> dict[str, Any] | str | None:
    """Validate and parse CLI ``--filter`` arguments against ``RecordsEndpoint`` allowlists."""
    if not filters:
        return None
    parsed = parse_filter_args(
        filters,
        model=Record,
        allowed_attributes=RecordsEndpoint.FILTER_ATTRIBUTES,
        disallow_study_key=True,
    )
    if (
        parsed is not None
        and len(parsed) == 1
        and "filter" in parsed
        and isinstance(parsed["filter"], str)
    ):
        return parsed["filter"]
    return parsed


def _extract_streaming_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """Extract non-default streaming/checkpoint options from parsed CLI args."""
    if getattr(args, "study_key", None) is not None:
        validate_cli_path_segment(args.study_key, name="study_key")
    opts: dict[str, Any] = {}
    since = getattr(args, "since", None)
    if since is not None:
        try:
            normalize_watermark_datetime(since)
        except ValueError as exc:
            print_error(f"Error: {exc}")
            sys.exit(1)
        opts["modified_since"] = since

    parsed_filter = _parse_export_filters(getattr(args, "filters", None))
    if parsed_filter is not None:
        opts["record_filter"] = parsed_filter

    record_data_filter = getattr(args, "record_data_filter", None)
    if record_data_filter is not None:
        validated_rdf = parse_record_data_filter(record_data_filter)
        if validated_rdf is not None:
            opts["record_data_filter"] = validated_rdf

    if getattr(args, "incremental", False):
        opts["incremental"] = True
    if getattr(args, "include_deleted", False):
        opts["include_deleted"] = True
    stream_name = getattr(args, "stream_name", None)
    if stream_name is not None:
        opts["stream_name"] = stream_name
    ledger_path = getattr(args, "ledger_path", None)
    if ledger_path is not None:
        opts["ledger_path"] = ledger_path
    if getattr(args, "mask_phi", True) is False:
        opts["mask_phi"] = False
    quality_gate = getattr(args, "quality_gate_enabled", None)
    if quality_gate is not None:
        opts["quality_gate_enabled"] = quality_gate
    min_readiness = getattr(args, "min_schema_readiness_score", None)
    if min_readiness is not None:
        opts["min_schema_readiness_score"] = min_readiness
    if getattr(args, "as_sdtm", False):
        opts["as_sdtm"] = True
    sdtm_domain = getattr(args, "sdtm_domain", None)
    if sdtm_domain is not None:
        opts["sdtm_domain"] = sdtm_domain
    standards_profile = getattr(args, "standards_profile", None)
    if standards_profile is not None:
        opts["standards_profile"] = standards_profile
    min_standards_score = getattr(args, "min_standards_score", None)
    if min_standards_score is not None:
        if min_standards_score < 0.0 or min_standards_score > 100.0:
            print_error("Error: --min-standards-score must be between 0 and 100.")
            sys.exit(1)
        opts["min_standards_score"] = min_standards_score
    naming_convention = getattr(args, "naming_convention", None)
    if naming_convention is not None:
        opts["naming_convention"] = naming_convention
    return opts


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("export", help="Export study data to various formats.")
    sub = parser.add_subparsers(dest="command")

    # parquet
    pq_parser = sub.add_parser("parquet", help="Export study records to a Parquet file.")
    _add_export_study_args(pq_parser)
    pq_parser.add_argument("path", type=Path, help="Destination Parquet file.")
    pq_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of names as column headers.",
    )
    _add_streaming_export_args(pq_parser)

    @with_sdk
    def export_parquet(
        sdk: ImednetSDK,
        /,
        study_key: str,
        path: Path,
        use_labels: bool = False,
        **stream_kwargs: Any,
    ) -> None:
        if importlib.util.find_spec("pyarrow") is None:
            print_error(
                "Error: pyarrow is required for Parquet export. Install with \"pip install 'imednet[export]'\"."
            )
            sys.exit(1)
        from .. import export_to_parquet

        with fetching_status("records for Parquet export", study_key):
            export_to_parquet(
                sdk,
                study_key,
                str(path),
                use_labels_as_columns=use_labels,
                **stream_kwargs,
            )

    def _dispatch_pq(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(pq_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_parquet(
                study_key=study_key,
                path=args.path,
                use_labels=args.use_labels,
                **stream_kwargs,
            )

    pq_parser.set_defaults(func=_dispatch_pq)

    # csv
    csv_parser = sub.add_parser("csv", help="Export study records to a CSV file.")
    _add_export_study_args(csv_parser)
    csv_parser.add_argument("path", type=Path, help="Destination CSV file.")
    csv_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of names as column headers.",
    )
    _add_streaming_export_args(csv_parser)

    @with_sdk
    def export_csv(
        sdk: ImednetSDK,
        /,
        study_key: str,
        path: Path,
        use_labels: bool = False,
        **stream_kwargs: Any,
    ) -> None:
        if importlib.util.find_spec("pandas") is None:
            print_error("Error: pandas is required for CSV export.")
            sys.exit(1)
        from .. import export_to_csv

        with fetching_status("records for CSV export", study_key):
            export_to_csv(
                sdk,
                study_key,
                str(path),
                use_labels_as_columns=use_labels,
                **stream_kwargs,
            )

    def _dispatch_csv(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(csv_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_csv(
                study_key=study_key,
                path=args.path,
                use_labels=args.use_labels,
                **stream_kwargs,
            )

    csv_parser.set_defaults(func=_dispatch_csv)

    # excel
    xl_parser = sub.add_parser("excel", help="Export study records to an Excel workbook.")
    _add_export_study_args(xl_parser)
    xl_parser.add_argument("path", type=Path, help="Destination Excel workbook.")
    xl_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of names as column headers.",
    )
    _add_streaming_export_args(xl_parser)

    @with_sdk
    def export_excel(
        sdk: ImednetSDK,
        /,
        study_key: str,
        path: Path,
        use_labels: bool = False,
        **stream_kwargs: Any,
    ) -> None:
        if (
            importlib.util.find_spec("pandas") is None
            or importlib.util.find_spec("openpyxl") is None
        ):
            print_error("Error: pandas and openpyxl are required for Excel export.")
            sys.exit(1)
        from .. import export_to_excel

        with fetching_status("records for Excel export", study_key):
            export_to_excel(
                sdk,
                study_key,
                str(path),
                use_labels_as_columns=use_labels,
                **stream_kwargs,
            )

    def _dispatch_xl(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(xl_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_excel(
                study_key=study_key,
                path=args.path,
                use_labels=args.use_labels,
                **stream_kwargs,
            )

    xl_parser.set_defaults(func=_dispatch_xl)

    # json
    json_parser = sub.add_parser("json", help="Export study records to a JSON file.")
    _add_export_study_args(json_parser)
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
    _add_streaming_export_args(json_parser)

    @with_sdk
    def export_json_cmd(
        sdk: ImednetSDK,
        /,
        study_key: str,
        path: Path,
        use_labels: bool = False,
        hierarchical: bool = False,
        **stream_kwargs: Any,
    ) -> None:
        from .. import export_to_json

        with fetching_status("records for JSON export", study_key):
            export_to_json(
                sdk,
                study_key,
                str(path),
                use_labels_as_columns=use_labels,
                hierarchical=hierarchical,
                **stream_kwargs,
            )

    def _dispatch_json(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(json_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_json_cmd(
                study_key=study_key,
                path=args.path,
                use_labels=args.use_labels,
                hierarchical=args.hierarchical,
                **stream_kwargs,
            )

    json_parser.set_defaults(func=_dispatch_json)

    # duckdb
    ddb_parser = sub.add_parser("duckdb", help="Export study records to a DuckDB table.")
    _add_export_study_args(ddb_parser)
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
    _add_streaming_export_args(ddb_parser)

    @with_sdk
    def export_duckdb(
        sdk: ImednetSDK,
        /,
        study_key: str,
        table_name: str,
        db_path: Path,
        vars_: str | None = None,
        forms: str | None = None,
        use_labels: bool = False,
        **stream_kwargs: Any,
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
                **stream_kwargs,
            )

    def _dispatch_ddb(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(ddb_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_duckdb(
                study_key=study_key,
                table_name=args.table_name,
                db_path=args.db_path,
                vars_=args.vars_,
                forms=args.forms,
                use_labels=args.use_labels,
                **stream_kwargs,
            )

    ddb_parser.set_defaults(func=_dispatch_ddb)

    # sql
    sql_parser = sub.add_parser("sql", help="Export study records to a SQL table.")
    _add_export_study_args(sql_parser)
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
    _add_streaming_export_args(sql_parser)

    @with_sdk
    def export_sql(
        sdk: ImednetSDK,
        /,
        study_key: str,
        table: str,
        connection_string: str,
        single_table: bool = False,
        long_format: bool = False,
        vars_: str | None = None,
        forms: str | None = None,
        **stream_kwargs: Any,
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
                export_to_long_sql(sdk, study_key, table, connection_string, **stream_kwargs)
                return
            if not single_table and engine.dialect.name == "sqlite":
                export_to_sql_by_form(
                    sdk,
                    study_key,
                    connection_string,
                    variable_whitelist=var_list,
                    form_whitelist=form_list,
                    **stream_kwargs,
                )
            else:
                export_to_sql(
                    sdk,
                    study_key,
                    table,
                    connection_string,
                    variable_whitelist=var_list,
                    form_whitelist=form_list,
                    **stream_kwargs,
                )

    def _dispatch_sql(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(sql_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_sql(
                study_key=study_key,
                table=args.table,
                connection_string=args.connection_string,
                single_table=args.single_table,
                long_format=args.long_format,
                vars_=args.vars_,
                forms=args.forms,
                **stream_kwargs,
            )

    sql_parser.set_defaults(func=_dispatch_sql)

    # mongodb
    m_parser = sub.add_parser("mongodb", help="Export study records to MongoDB document envelopes.")
    _add_export_study_args(m_parser)
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
    _add_streaming_export_args(m_parser)

    @with_sdk
    def export_mongodb(
        sdk: ImednetSDK,
        /,
        study_key: str,
        uri: str,
        database: str,
        collection: str,
        batch_size: int = 500,
        upsert: bool = True,
        **stream_kwargs: Any,
    ) -> None:
        if importlib.util.find_spec("pymongo") is None:
            print_error(
                "Error: pymongo is required for MongoDB export. Install with \"pip install 'imednet[mongodb]'\""
            )
            sys.exit(1)
        assert sdk.sinks is not None
        cfg = sdk.sinks.MongoDbSinkConfig(
            study_key=study_key,
            uri=uri,
            database=database,
            collection=collection,
            batch_size=batch_size,
            idempotent=upsert,
            **stream_kwargs,
        )
        with fetching_status("records for MongoDB export", study_key):
            sdk.sinks.export_to_mongodb(study_key, config=cfg)

    def _dispatch_mongodb(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(m_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_mongodb(
                study_key=study_key,
                uri=args.uri,
                database=args.database,
                collection=args.collection,
                batch_size=args.batch_size,
                upsert=args.upsert,
                **stream_kwargs,
            )

    m_parser.set_defaults(func=_dispatch_mongodb)

    # neo4j
    n_parser = sub.add_parser(
        "neo4j", help="Export study records to Neo4j nodes and relationships."
    )
    _add_export_study_args(n_parser)
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
    _add_streaming_export_args(n_parser)

    @with_sdk
    def export_neo4j(
        sdk: ImednetSDK,
        /,
        study_key: str,
        uri: str,
        username: str,
        password: str,
        database: str = "neo4j",
        batch_size: int = 500,
        merge: bool = True,
        **stream_kwargs: Any,
    ) -> None:
        if importlib.util.find_spec("neo4j") is None:
            print_error(
                "Error: neo4j is required for Neo4j export. Install with \"pip install 'imednet[neo4j]'\""
            )
            sys.exit(1)
        assert sdk.sinks is not None
        cfg = sdk.sinks.Neo4jSinkConfig(
            study_key=study_key,
            uri=uri,
            auth=(username, password),
            batch_size=batch_size,
            idempotent=merge,
            database=database,
            **stream_kwargs,
        )
        with fetching_status("records for Neo4j export", study_key):
            sdk.sinks.export_to_neo4j(study_key, config=cfg)

    def _dispatch_neo4j(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(n_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_neo4j(
                study_key=study_key,
                uri=args.uri,
                username=args.username,
                password=args.password,
                database=args.database,
                batch_size=args.batch_size,
                merge=args.merge,
                **stream_kwargs,
            )

    n_parser.set_defaults(func=_dispatch_neo4j)

    # snowflake
    s_parser = sub.add_parser(
        "snowflake", help="Export study records to Snowflake using staged Parquet + COPY INTO."
    )
    _add_export_study_args(s_parser)
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
    _add_streaming_export_args(s_parser)

    @with_sdk
    def export_snowflake(
        sdk: ImednetSDK,
        /,
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
        **stream_kwargs: Any,
    ) -> None:
        if (
            importlib.util.find_spec("snowflake.connector") is None
            or importlib.util.find_spec("pyarrow") is None
        ):
            print_error(
                "Error: snowflake-connector-python and pyarrow are required for Snowflake export. Install with \"pip install 'imednet[snowflake]'\""
            )
            sys.exit(1)
        assert sdk.sinks is not None
        cfg = sdk.sinks.SnowflakeSinkConfig(
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
            **stream_kwargs,
        )
        with fetching_status("records for Snowflake export", study_key):
            sdk.sinks.export_to_snowflake(study_key, config=cfg)

    def _dispatch_snowflake(args: argparse.Namespace) -> None:
        study_key = _resolve_export_study(s_parser, args)
        stream_kwargs = _extract_streaming_kwargs(args)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            export_snowflake(
                study_key=study_key,
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
                **stream_kwargs,
            )

    s_parser.set_defaults(func=_dispatch_snowflake)
