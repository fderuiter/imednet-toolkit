"""Unit tests for discovery helpers."""

from unittest.mock import MagicMock

import pytest

from imednet.discovery import (
    NoLiveDataError,
    discover_form_key,
    discover_interval_name,
    discover_site_name,
    discover_study_key,
    discover_subject_key,
)


def test_discover_study_key_returns_first() -> None:
    """Test that discover study key returns first."""
    sdk = MagicMock()
    sdk.studies.list.return_value = [
        MagicMock(study_key="S1"),
        MagicMock(study_key="S2"),
    ]

    assert discover_study_key(sdk) == "S1"
    sdk.studies.list.assert_called_once_with()


def test_discover_study_key_raises_when_empty() -> None:
    """Test that discover study key raises when empty."""
    sdk = MagicMock()
    sdk.studies.list.return_value = []

    with pytest.raises(NoLiveDataError):
        discover_study_key(sdk)


def test_discover_form_key_returns_first_matching() -> None:
    """Test that discover form key returns first matching."""
    sdk = MagicMock()
    sdk.forms.list.return_value = [
        MagicMock(subject_record_report=False),
        MagicMock(form_key="F1", subject_record_report=True, disabled=False),
        MagicMock(form_key="F2", subject_record_report=True, disabled=False),
    ]
    sdk.variables.list.return_value = [MagicMock()]

    assert discover_form_key(sdk, "S") == "F1"
    sdk.forms.list.assert_called_once_with(study_key="S")


def test_discover_form_key_raises_when_empty() -> None:
    """Test that discover form key raises when empty."""
    sdk = MagicMock()
    sdk.forms.list.return_value = []
    sdk.variables.list.return_value = [MagicMock()]

    with pytest.raises(NoLiveDataError):
        discover_form_key(sdk, "S")


def test_discover_site_name_returns_first_active() -> None:
    """Test that discover site name returns first active."""
    sdk = MagicMock()
    sdk.sites.list.return_value = [
        MagicMock(site_enrollment_status="Closed"),
        MagicMock(site_name="Open", site_enrollment_status="Active"),
    ]

    assert discover_site_name(sdk, "S") == "Open"
    sdk.sites.list.assert_called_once_with(study_key="S")


def test_discover_site_name_returns_enrollment_open() -> None:
    """ENROLLMENT_OPEN sites should be considered eligible."""
    sdk = MagicMock()
    sdk.sites.list.return_value = [
        MagicMock(site_name="EnrollOpen", site_enrollment_status="ENROLLMENT_OPEN"),
    ]

    assert discover_site_name(sdk, "S") == "EnrollOpen"


def test_discover_site_name_raises_when_empty() -> None:
    """Test that discover site name raises when empty."""
    sdk = MagicMock()
    sdk.sites.list.return_value = []

    with pytest.raises(NoLiveDataError):
        discover_site_name(sdk, "S")


def test_discover_site_name_raises_with_read_only_sites() -> None:
    """READ_ONLY sites alone should not satisfy eligibility."""
    sdk = MagicMock()
    sdk.sites.list.return_value = [
        MagicMock(site_name="RO", site_enrollment_status="READ_ONLY"),
    ]

    with pytest.raises(NoLiveDataError, match="READ_ONLY"):
        discover_site_name(sdk, "S")


def test_discover_subject_key_returns_first_active() -> None:
    """Test that discover subject key returns first active."""
    sdk = MagicMock()
    sdk.subjects.list.return_value = [
        MagicMock(subject_status="Closed"),
        MagicMock(subject_key="S2", subject_status="Active"),
    ]

    assert discover_subject_key(sdk, "S") == "S2"
    sdk.subjects.list.assert_called_once_with(study_key="S")


def test_discover_subject_key_returns_registered() -> None:
    """Registered subjects should be considered eligible."""
    sdk = MagicMock()
    sdk.subjects.list.return_value = [
        MagicMock(subject_key="S1", subject_status="Registered"),
    ]

    assert discover_subject_key(sdk, "S") == "S1"


def test_discover_subject_key_returns_baseline() -> None:
    """Baseline subjects should be considered eligible."""
    sdk = MagicMock()
    sdk.subjects.list.return_value = [
        MagicMock(subject_key="S1", subject_status="Baseline"),
    ]

    assert discover_subject_key(sdk, "S") == "S1"


def test_discover_subject_key_returns_enrolled() -> None:
    """Enrolled subjects should be considered eligible."""
    sdk = MagicMock()
    sdk.subjects.list.return_value = [
        MagicMock(subject_key="S1", subject_status="Enrolled"),
    ]

    assert discover_subject_key(sdk, "S") == "S1"


def test_discover_subject_key_raises_when_empty() -> None:
    """Test that discover subject key raises when empty."""
    sdk = MagicMock()
    sdk.subjects.list.return_value = []

    with pytest.raises(NoLiveDataError):
        discover_subject_key(sdk, "S")


def test_discover_subject_key_raises_with_encountered_statuses() -> None:
    """Error message should include encountered statuses for diagnostics."""
    sdk = MagicMock()
    sdk.subjects.list.return_value = [
        MagicMock(subject_status="Closed"),
        MagicMock(subject_status="Withdrawn"),
    ]

    with pytest.raises(NoLiveDataError, match="Closed|Withdrawn"):
        discover_subject_key(sdk, "S")


def test_discover_interval_name_returns_first_enabled() -> None:
    """Test that discover interval name returns first enabled."""
    sdk = MagicMock()
    sdk.intervals.list.return_value = [
        MagicMock(disabled=True),
        MagicMock(interval_name="I2", disabled=False),
    ]

    assert discover_interval_name(sdk, "S") == "I2"
    sdk.intervals.list.assert_called_once_with(study_key="S")


def test_discover_interval_name_raises_when_empty() -> None:
    """Test that discover interval name raises when empty."""
    sdk = MagicMock()
    sdk.intervals.list.return_value = []

    with pytest.raises(NoLiveDataError):
        discover_interval_name(sdk, "S")


def test_discover_site_name_case_insensitive() -> None:
    """Status matching must be case-insensitive per the live-test charter."""
    sdk = MagicMock()
    sdk.sites.list.return_value = [
        MagicMock(site_name="Lower", site_enrollment_status="active"),
        MagicMock(site_name="Upper", site_enrollment_status="ACTIVE"),
        MagicMock(site_name="Mixed", site_enrollment_status="Active"),
    ]

    assert discover_site_name(sdk, "S") == "Lower"


def test_discover_subject_key_case_insensitive() -> None:
    """Status matching must be case-insensitive per the live-test charter."""
    sdk = MagicMock()
    sdk.subjects.list.return_value = [
        MagicMock(subject_key="S1", subject_status="ACTIVE"),
        MagicMock(subject_key="S2", subject_status="Active"),
    ]

    assert discover_subject_key(sdk, "S") == "S1"


def test_discover_site_name_raises_when_none_active() -> None:
    """Sites with no active status should raise even when the list is non-empty."""
    sdk = MagicMock()
    sdk.sites.list.return_value = [
        MagicMock(site_enrollment_status="Closed"),
        MagicMock(site_enrollment_status="Inactive"),
    ]

    with pytest.raises(NoLiveDataError, match="No eligible sites"):
        discover_site_name(sdk, "S")


def test_discover_subject_key_raises_when_none_active() -> None:
    """Subjects with no active status should raise even when the list is non-empty."""
    sdk = MagicMock()
    sdk.subjects.list.return_value = [
        MagicMock(subject_status="Withdrawn"),
        MagicMock(subject_status="Inactive"),
    ]

    with pytest.raises(NoLiveDataError, match="No eligible subjects"):
        discover_subject_key(sdk, "S")


def test_discover_site_name_accepts_natural_enrollment_open_site_model() -> None:
    """Site with 'Enrollment Open' status is eligible and returned by discover_site_name."""
    from imednet.discovery import is_site_eligible
    from imednet.models.sites import Site

    assert is_site_eligible("Enrollment Open") is True
    assert is_site_eligible("Enrollment Closed") is False

    sdk = MagicMock()
    sdk.sites.list.return_value = [
        Site(
            studyKey="S", siteId=1, siteName="ClosedSite", siteEnrollmentStatus="Enrollment Closed"
        ),
        Site(studyKey="S", siteId=2, siteName="OpenSite", siteEnrollmentStatus="Enrollment Open"),
    ]

    assert discover_site_name(sdk, "S") == "OpenSite"


def test_discover_subject_key_skips_deleted_subjects() -> None:
    """discover_subject_key skips subjects where deleted=True even if subjectStatus is Enrolled."""
    from imednet.models.subjects import Subject

    sdk = MagicMock()
    sdk.subjects.list.return_value = [
        Subject(studyKey="S", subjectKey="DEL-1", deleted=True, subjectStatus="Enrolled"),
        Subject(studyKey="S", subjectKey="LIVE-2", deleted=False, subjectStatus="Enrolled"),
    ]

    assert discover_subject_key(sdk, "S") == "LIVE-2"

    sdk_only_deleted = MagicMock()
    sdk_only_deleted.subjects.list.return_value = [
        Subject(studyKey="S", subjectKey="DEL-1", deleted=True, subjectStatus="Enrolled"),
    ]
    with pytest.raises(NoLiveDataError, match="DELETED"):
        discover_subject_key(sdk_only_deleted, "S")
