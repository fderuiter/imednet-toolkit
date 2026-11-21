# pylint: disable=duplicate-code
"""Endpoints package for the iMedNet SDK.

This package contains all API endpoint implementations for accessing iMedNet resources.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imednet.endpoints.codings import AsyncCodingsEndpoint, CodingsEndpoint
    from imednet.endpoints.forms import AsyncFormsEndpoint, FormsEndpoint
    from imednet.endpoints.intervals import AsyncIntervalsEndpoint, IntervalsEndpoint
    from imednet.endpoints.jobs import AsyncJobsEndpoint, JobsEndpoint
    from imednet.endpoints.queries import AsyncQueriesEndpoint, QueriesEndpoint
    from imednet.endpoints.record_revisions import (
        AsyncRecordRevisionsEndpoint,
        RecordRevisionsEndpoint,
    )
    from imednet.endpoints.records import AsyncRecordsEndpoint, RecordsEndpoint
    from imednet.endpoints.sites import AsyncSitesEndpoint, SitesEndpoint
    from imednet.endpoints.studies import AsyncStudiesEndpoint, StudiesEndpoint
    from imednet.endpoints.subjects import AsyncSubjectsEndpoint, SubjectsEndpoint
    from imednet.endpoints.users import AsyncUsersEndpoint, UsersEndpoint
    from imednet.endpoints.variables import AsyncVariablesEndpoint, VariablesEndpoint
    from imednet.endpoints.visits import AsyncVisitsEndpoint, VisitsEndpoint

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "AsyncCodingsEndpoint": ("imednet.endpoints.codings", "AsyncCodingsEndpoint"),
    "AsyncFormsEndpoint": ("imednet.endpoints.forms", "AsyncFormsEndpoint"),
    "AsyncIntervalsEndpoint": ("imednet.endpoints.intervals", "AsyncIntervalsEndpoint"),
    "AsyncJobsEndpoint": ("imednet.endpoints.jobs", "AsyncJobsEndpoint"),
    "AsyncQueriesEndpoint": ("imednet.endpoints.queries", "AsyncQueriesEndpoint"),
    "AsyncRecordRevisionsEndpoint": (
        "imednet.endpoints.record_revisions",
        "AsyncRecordRevisionsEndpoint",
    ),
    "AsyncRecordsEndpoint": ("imednet.endpoints.records", "AsyncRecordsEndpoint"),
    "AsyncSitesEndpoint": ("imednet.endpoints.sites", "AsyncSitesEndpoint"),
    "AsyncStudiesEndpoint": ("imednet.endpoints.studies", "AsyncStudiesEndpoint"),
    "AsyncSubjectsEndpoint": ("imednet.endpoints.subjects", "AsyncSubjectsEndpoint"),
    "AsyncUsersEndpoint": ("imednet.endpoints.users", "AsyncUsersEndpoint"),
    "AsyncVariablesEndpoint": ("imednet.endpoints.variables", "AsyncVariablesEndpoint"),
    "AsyncVisitsEndpoint": ("imednet.endpoints.visits", "AsyncVisitsEndpoint"),
    "CodingsEndpoint": ("imednet.endpoints.codings", "CodingsEndpoint"),
    "FormsEndpoint": ("imednet.endpoints.forms", "FormsEndpoint"),
    "IntervalsEndpoint": ("imednet.endpoints.intervals", "IntervalsEndpoint"),
    "JobsEndpoint": ("imednet.endpoints.jobs", "JobsEndpoint"),
    "QueriesEndpoint": ("imednet.endpoints.queries", "QueriesEndpoint"),
    "RecordRevisionsEndpoint": ("imednet.endpoints.record_revisions", "RecordRevisionsEndpoint"),
    "RecordsEndpoint": ("imednet.endpoints.records", "RecordsEndpoint"),
    "SitesEndpoint": ("imednet.endpoints.sites", "SitesEndpoint"),
    "StudiesEndpoint": ("imednet.endpoints.studies", "StudiesEndpoint"),
    "SubjectsEndpoint": ("imednet.endpoints.subjects", "SubjectsEndpoint"),
    "UsersEndpoint": ("imednet.endpoints.users", "UsersEndpoint"),
    "VariablesEndpoint": ("imednet.endpoints.variables", "VariablesEndpoint"),
    "VisitsEndpoint": ("imednet.endpoints.visits", "VisitsEndpoint"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve endpoint classes on demand."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy exports."""
    return sorted(set(globals()) | set(__all__))


__all__: list[str] = [
    "AsyncCodingsEndpoint",
    "AsyncFormsEndpoint",
    "AsyncIntervalsEndpoint",
    "AsyncJobsEndpoint",
    "AsyncQueriesEndpoint",
    "AsyncRecordRevisionsEndpoint",
    "AsyncRecordsEndpoint",
    "AsyncSitesEndpoint",
    "AsyncStudiesEndpoint",
    "AsyncSubjectsEndpoint",
    "AsyncUsersEndpoint",
    "AsyncVariablesEndpoint",
    "AsyncVisitsEndpoint",
    "CodingsEndpoint",
    "FormsEndpoint",
    "IntervalsEndpoint",
    "JobsEndpoint",
    "QueriesEndpoint",
    "RecordRevisionsEndpoint",
    "RecordsEndpoint",
    "SitesEndpoint",
    "StudiesEndpoint",
    "SubjectsEndpoint",
    "UsersEndpoint",
    "VariablesEndpoint",
    "VisitsEndpoint",
]
