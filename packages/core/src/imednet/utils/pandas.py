"""Pandas helpers for working with iMednet models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    import pandas as pd
except ImportError:
    pd: Any = None  # type: ignore[no-redef]
from ..models.forms import Form
from ..models.intervals import Interval
from ..models.queries import Query
from ..models.records import Record
from ..models.sites import Site
from ..models.subjects import Subject
from ..models.users import User
from ..models.variables import Variable
from ..models.visits import Visit
from .security import sanitize_csv_formula

if TYPE_CHECKING:  # pragma: no cover - only for type checking
    from ..sdk import ImednetSDK


def check_pandas(feature_name: str | None = None) -> None:
    """Validate that pandas is installed, raising an ImportError if missing.

    Args:
        feature_name: Optional name of the feature or function requiring pandas.

    Raises:
        ImportError: If pandas is not installed.
    """
    if pd is None:
        target = f" for {feature_name}" if feature_name else ""
        raise ImportError(
            f"pandas is required{target}. Install with \"pip install 'imednet[export]'\"."
        )


def queries_to_dataframe(queries: list[Query]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.queries.Query` to a DataFrame.

    Each query model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas("queries_to_dataframe")

    query_dicts = [q.model_dump(by_alias=False) for q in queries]
    return pd.DataFrame(query_dicts)


def variables_to_dataframe(variables: list[Variable]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.variables.Variable` to a DataFrame.

    Each variable model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas("variables_to_dataframe")

    variable_dicts = [v.model_dump(by_alias=False) for v in variables]
    return pd.DataFrame(variable_dicts)


def subjects_to_dataframe(subjects: list[Subject]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.subjects.Subject` to a DataFrame.

    Each subject model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas("subjects_to_dataframe")

    subject_dicts = [s.model_dump(by_alias=False) for s in subjects]
    return pd.DataFrame(subject_dicts)


def forms_to_dataframe(forms: list[Form]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.forms.Form` to a DataFrame.

    Each form model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas("forms_to_dataframe")

    form_dicts = [f.model_dump(by_alias=False) for f in forms]
    return pd.DataFrame(form_dicts)


def visits_to_dataframe(visits: list[Visit]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.visits.Visit` to a DataFrame.

    Each visit model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas("visits_to_dataframe")

    visit_dicts = [v.model_dump(by_alias=False) for v in visits]
    return pd.DataFrame(visit_dicts)


def intervals_to_dataframe(intervals: list[Interval]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.intervals.Interval` to a DataFrame.

    Each interval model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas("intervals_to_dataframe")

    interval_dicts = [i.model_dump(by_alias=False) for i in intervals]
    return pd.DataFrame(interval_dicts)


def sites_to_dataframe(sites: list[Site]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.sites.Site` to a DataFrame.

    Each site model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    check_pandas("sites_to_dataframe")

    site_dicts = [s.model_dump(by_alias=False) for s in sites]
    return pd.DataFrame(site_dicts)


def users_to_dataframe(users: list[User]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.users.User` to a DataFrame.

    Each user model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for users_to_dataframe. Install with \"pip install 'imednet[export]'\"."
        )

    user_dicts = [u.model_dump(by_alias=False) for u in users]
    return pd.DataFrame(user_dicts)


def records_to_dataframe(records: list[Record], *, flatten: bool = False) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.records.Record` to a DataFrame.

    Each record is converted using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False``. If ``flatten`` is ``True`` the ``record_data`` column is
    expanded using :func:`pandas.json_normalize` so that each variable becomes a
    column in the resulting DataFrame.
    """
    check_pandas("records_to_dataframe")

    rows = [r.model_dump(by_alias=False) for r in records]
    df = pd.DataFrame(rows)
    if flatten and not df.empty:
        record_df = pd.json_normalize(df["record_data"].tolist(), sep="_")
        df = pd.concat([df.drop(columns=["record_data"]), record_df], axis=1)
    return df


def export_records_csv(
    sdk: ImednetSDK, study_key: str, file_path: str, *, flatten: bool = True
) -> None:
    """Fetch all records for ``study_key`` and write them to ``file_path``.

    Parameters are passed to :func:`records_to_dataframe` and the resulting
    DataFrame is written with :meth:`pandas.DataFrame.to_csv` using
    ``index=False``.
    """
    check_pandas("export_records_csv")

    records = list(sdk.records.list(study_key=study_key))
    df = records_to_dataframe(records, flatten=flatten)

    # Sanitize data to prevent CSV injection
    df = df.map(sanitize_csv_formula)

    df.to_csv(file_path, index=False)
