"""Unit tests for lazy Workflows, AsyncWorkflows, and create_workflows factory."""

from __future__ import annotations

import pytest

from imednet.plugins import AsyncWorkflowsNamespaceProtocol, WorkflowsNamespaceProtocol
from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow
from imednet_workflows.namespace import AsyncWorkflows, Workflows, create_workflows
from imednet_workflows.query_management import QueryManagementWorkflow
from imednet_workflows.record_mapper import RecordMapper
from imednet_workflows.record_update import RecordUpdateWorkflow
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow
from imednet_workflows.subject_data import SubjectDataWorkflow
from imednet_workflows.uat import StudySchemaInspector, UATWorkflow


def test_workflows_lazy_properties_and_caching() -> None:
    """Workflows lazily instantiates and caches all 9 workflow attributes on ImednetSDK."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://example.com")
    ns = create_workflows(sdk)
    assert isinstance(ns, Workflows)
    assert isinstance(ns, WorkflowsNamespaceProtocol)
    assert vars(ns) == {"_sdk": sdk}

    expected_types = {
        "data_extraction": DataExtractionWorkflow,
        "duckdb_centralizer": DuckDBIngestionWorkflow,
        "query_management": QueryManagementWorkflow,
        "record_mapper": RecordMapper,
        "record_update": RecordUpdateWorkflow,
        "register_subjects": RegisterSubjectsWorkflow,
        "subject_data": SubjectDataWorkflow,
        "uat": UATWorkflow,
        "uat_inspector": StudySchemaInspector,
    }
    for attr_name, cls in expected_types.items():
        first = getattr(ns, attr_name)
        second = getattr(ns, attr_name)
        assert isinstance(first, cls)
        assert first is second


@pytest.mark.asyncio
async def test_async_workflows_namespace_and_sync_only_guards() -> None:
    """AsyncWorkflows exposes async-capable workflows and rejects sync-only workflows."""
    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
    ) as async_sdk:
        ns = create_workflows(async_sdk)
        assert isinstance(ns, AsyncWorkflows)
        assert isinstance(ns, AsyncWorkflowsNamespaceProtocol)
        assert not isinstance(ns, WorkflowsNamespaceProtocol)
        assert vars(ns) == {"_sdk": async_sdk}

        assert isinstance(ns.record_update, RecordUpdateWorkflow)
        assert ns.record_update is ns.record_update
        assert isinstance(ns.uat_inspector, StudySchemaInspector)
        assert ns.uat_inspector is ns.uat_inspector

        for sync_only in (
            "data_extraction",
            "duckdb_centralizer",
            "query_management",
            "record_mapper",
            "register_subjects",
            "subject_data",
            "uat",
        ):
            with pytest.raises(TypeError, match="synchronous-only"):
                getattr(ns, sync_only)

        with pytest.raises(AttributeError, match="no attribute 'does_not_exist'"):
            _ = ns.does_not_exist

        with pytest.raises(TypeError, match="synchronous ImednetSDK"):
            Workflows(async_sdk)  # type: ignore[arg-type]

        for sync_cls in (
            DataExtractionWorkflow,
            DuckDBIngestionWorkflow,
            QueryManagementWorkflow,
            RecordMapper,
            RegisterSubjectsWorkflow,
            SubjectDataWorkflow,
            UATWorkflow,
        ):
            with pytest.raises(TypeError, match="AsyncImednetSDK"):
                sync_cls(async_sdk)  # type: ignore[arg-type]


def test_async_workflows_rejects_sync_sdk() -> None:
    """AsyncWorkflows raises TypeError if constructed directly with synchronous ImednetSDK."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://example.com")
    with pytest.raises(TypeError, match="AsyncImednetSDK"):
        AsyncWorkflows(sdk)  # type: ignore[arg-type]
