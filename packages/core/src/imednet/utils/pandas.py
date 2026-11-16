"""Pandas helpers for working with iMednet models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    import pandas as pd
except ImportError:
    pd: Any = None  # type: ignore[no-redef]
from ..models.records import Record
from ..models.subjects import Subject
from ..models.variables import Variable
from .security import sanitize_csv_formula

if TYPE_CHECKING:  # pragma: no cover - only for type checking
    from ..sdk import ImednetSDK


def variables_to_dataframe(variables: list[Variable]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.variables.Variable` to a DataFrame.

    Each variable model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for variables_to_dataframe. Install with \"pip install 'imednet[export]'\"."
        )

    variable_dicts = [v.model_dump(by_alias=False) for v in variables]
    return pd.DataFrame(variable_dicts)


def subjects_to_dataframe(subjects: list[Subject]) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.subjects.Subject` to a DataFrame.

    Each subject model is serialized using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False`` and returned as a :class:`pandas.DataFrame`.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for subjects_to_dataframe. Install with \"pip install 'imednet[export]'\"."
        )

    subject_dicts = [s.model_dump(by_alias=False) for s in subjects]
    return pd.DataFrame(subject_dicts)


def records_to_dataframe(records: list[Record], *, flatten: bool = False) -> pd.DataFrame:
    """Convert a list of :class:`~imednet.models.records.Record` to a DataFrame.

    Each record is converted using :meth:`pydantic.BaseModel.model_dump` with
    ``by_alias=False``. If ``flatten`` is ``True`` the ``record_data`` column is
    expanded using :func:`pandas.json_normalize` so that each variable becomes a
    column in the resulting DataFrame.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for records_to_dataframe. Install with \"pip install 'imednet[export]'\"."
        )

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
    if pd is None:
        raise ImportError(
            "pandas is required for export_records_csv. Install with \"pip install 'imednet[export]'\"."
        )

    records = list(sdk.records.list(study_key=study_key))
    df = records_to_dataframe(records, flatten=flatten)

    # Sanitize data to prevent CSV injection
    df = df.map(sanitize_csv_formula)

    df.to_csv(file_path, index=False)
