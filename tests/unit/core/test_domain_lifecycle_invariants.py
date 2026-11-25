"""Unit tests for domain lifecycle invariants across Site, Subject, Record, and RecordRevision."""

from __future__ import annotations

import pytest

from imednet.models import (
    ALLOWED_RECORD_TRANSITIONS,
    ALLOWED_SUBJECT_TRANSITIONS,
    ELIGIBLE_SITE_ENROLLMENT_STATUSES,
    Record,
    RecordRevision,
    RecordStatus,
    Site,
    SiteEnrollmentStatus,
    Subject,
    SubjectStatus,
    is_site_enrollment_eligible,
    is_valid_record_revision_chain,
    is_valid_record_transition,
    is_valid_subject_transition,
    normalize_record_status,
    normalize_site_enrollment_status,
    normalize_subject_status,
    validate_record_revision_chain,
    validate_record_transition,
    validate_subject_transition,
)
from imednet.spi import models as spi_models
from imednet.testing.fake_data import (
    fake_record,
    fake_record_revision,
    fake_site,
    fake_subject,
)
from imednet.validation.cache import SchemaCache


def test_spi_models_exports_lifecycle_symbols() -> None:
    """Ensure SPI models re-export normalized status enums and validators."""
    assert spi_models.SiteEnrollmentStatus is SiteEnrollmentStatus
    assert spi_models.SubjectStatus is SubjectStatus
    assert spi_models.RecordStatus is RecordStatus
    assert spi_models.validate_record_revision_chain is validate_record_revision_chain


def test_site_enrollment_status_normalization_and_eligibility() -> None:
    """Verify SiteEnrollmentStatus enum values, normalization, and eligibility."""
    assert str(SiteEnrollmentStatus.ENROLLMENT_OPEN) == "Enrollment Open"
    assert SiteEnrollmentStatus("enrollment_open") is SiteEnrollmentStatus.ENROLLMENT_OPEN
    assert SiteEnrollmentStatus("Enrollment Open") is SiteEnrollmentStatus.ENROLLMENT_OPEN
    assert SiteEnrollmentStatus("enrollment-closed") is SiteEnrollmentStatus.ENROLLMENT_CLOSED
    assert SiteEnrollmentStatus("READ_ONLY") is SiteEnrollmentStatus.READ_ONLY

    assert SiteEnrollmentStatus.ACTIVE in ELIGIBLE_SITE_ENROLLMENT_STATUSES
    assert SiteEnrollmentStatus.ENROLLMENT_OPEN.is_eligible is True
    assert SiteEnrollmentStatus.ENROLLMENT_CLOSED.is_eligible is False

    assert normalize_site_enrollment_status(None) is None
    assert (
        normalize_site_enrollment_status("Enrollment Open") is SiteEnrollmentStatus.ENROLLMENT_OPEN
    )
    assert normalize_site_enrollment_status("<string>") == "<string>"
    assert is_site_enrollment_eligible("Enrollment Open") is True
    assert is_site_enrollment_eligible("Active") is True
    assert is_site_enrollment_eligible("Enrollment Closed") is False
    assert is_site_enrollment_eligible(None) is False

    open_site = Site.model_validate(
        {
            "studyKey": "S1",
            "siteId": 1,
            "siteName": "Site A",
            "siteEnrollmentStatus": "Enrollment Open",
        }
    )
    assert open_site.site_enrollment_status == SiteEnrollmentStatus.ENROLLMENT_OPEN
    assert open_site.site_enrollment_status == "Enrollment Open"
    assert open_site.is_enrollment_open is True
    assert open_site.is_eligible is True

    custom_site = Site.model_validate(
        {"studyKey": "S1", "siteId": 2, "siteName": "Site B", "siteEnrollmentStatus": "<string>"}
    )
    assert custom_site.site_enrollment_status == "<string>"
    assert custom_site.is_enrollment_open is False
    assert custom_site.is_eligible is False

    generated_site = Site.model_validate(fake_site())
    assert isinstance(generated_site.site_enrollment_status, SiteEnrollmentStatus)


def test_subject_status_transitions_and_soft_delete_invariants() -> None:
    """Verify SubjectStatus normalization, transitions, and soft-delete guards."""
    assert str(SubjectStatus.ENROLLED) == "Enrolled"
    assert SubjectStatus("screen_failure") is SubjectStatus.SCREEN_FAILED
    assert SubjectStatus("Screen Failure") is SubjectStatus.SCREEN_FAILED
    assert SubjectStatus("DELETED") is SubjectStatus.DELETED
    assert normalize_subject_status(None) is None
    assert normalize_subject_status("Custom Hold") == "Custom Hold"

    assert SubjectStatus.ACTIVE in ALLOWED_SUBJECT_TRANSITIONS[SubjectStatus.ENROLLED]
    assert SubjectStatus.DELETED.allowed_transitions == set()
    assert SubjectStatus.ENROLLED.can_transition_to(SubjectStatus.COMPLETED) is True
    assert SubjectStatus.DELETED.can_transition_to(SubjectStatus.ACTIVE) is False
    assert SubjectStatus.ENROLLED.validate_transition_to(SubjectStatus.WITHDRAWN) is True
    with pytest.raises(ValueError, match="Cannot transition soft-deleted Subject from DELETED"):
        SubjectStatus.DELETED.validate_transition_to(SubjectStatus.ACTIVE)

    assert is_valid_subject_transition("Enrolled", "Completed") is True
    assert is_valid_subject_transition("Deleted", "Active") is False
    assert is_valid_subject_transition("Custom A", "Custom B") is True
    assert is_valid_subject_transition("Custom A", "Custom A") is False
    assert validate_subject_transition("Custom A", "Custom B") is True
    with pytest.raises(ValueError, match="Cannot transition subject status"):
        validate_subject_transition("Custom A", "Custom A")

    # Deserializing a deleted subject with historical active status succeeds
    deleted_subj = Subject.model_validate(
        {"studyKey": "S1", "subjectKey": "SUBJ-1", "deleted": True, "subjectStatus": "Enrolled"}
    )
    assert deleted_subj.is_deleted is True
    assert deleted_subj.effective_status == SubjectStatus.DELETED
    assert deleted_subj.can_transition_to(SubjectStatus.ACTIVE) is False

    with pytest.raises(ValueError, match="soft-deleted Subject"):
        deleted_subj.transition_to(SubjectStatus.ACTIVE)
    with pytest.raises(ValueError, match="soft-deleted Subject"):
        deleted_subj.update_status("Completed")
    with pytest.raises(ValueError, match="soft-deleted Subject"):
        deleted_subj.subject_status = "Active"
    with pytest.raises(ValueError, match="Cannot restore a soft-deleted Subject"):
        deleted_subj.deleted = False

    # Non-deleted lifecycle transitions
    active_subj = Subject.model_validate(
        {"studyKey": "S1", "subjectKey": "SUBJ-2", "deleted": False, "subjectStatus": "Screened"}
    )
    assert active_subj.is_deleted is False
    assert active_subj.effective_status == SubjectStatus.SCREENED
    assert active_subj.can_transition_to(SubjectStatus.ENROLLED) is True
    active_subj.transition_to(SubjectStatus.ENROLLED)
    assert active_subj.subject_status == SubjectStatus.ENROLLED
    active_subj.update_status(SubjectStatus.DELETED)
    assert active_subj.deleted is True
    assert active_subj.effective_status == SubjectStatus.DELETED

    generated_subject = Subject.model_validate(fake_subject())
    assert isinstance(generated_subject.subject_status, SubjectStatus)


def test_record_status_transitions_and_soft_delete_invariants() -> None:
    """Verify RecordStatus normalization, transitions, and soft-delete guards."""
    assert str(RecordStatus.RECORD_COMPLETE) == "Record Complete"
    assert RecordStatus("record_incomplete") is RecordStatus.RECORD_INCOMPLETE
    assert RecordStatus("Record Incomplete") is RecordStatus.RECORD_INCOMPLETE
    assert RecordStatus("incomplete") is RecordStatus.INCOMPLETE
    assert RecordStatus("DELETED") is RecordStatus.DELETED
    assert normalize_record_status(None) is None
    assert normalize_record_status("Custom Workflow") == "Custom Workflow"

    assert (
        RecordStatus.RECORD_COMPLETE in ALLOWED_RECORD_TRANSITIONS[RecordStatus.RECORD_INCOMPLETE]
    )
    assert RecordStatus.DELETED.allowed_transitions == set()
    assert RecordStatus.RECORD_INCOMPLETE.can_transition_to(RecordStatus.RECORD_COMPLETE) is True
    assert RecordStatus.DELETED.can_transition_to(RecordStatus.RECORD_COMPLETE) is False
    assert RecordStatus.RECORD_COMPLETE.validate_transition_to(RecordStatus.LOCKED) is True
    with pytest.raises(ValueError, match="Cannot transition soft-deleted Record from DELETED"):
        RecordStatus.DELETED.validate_transition_to(RecordStatus.RECORD_COMPLETE)

    assert is_valid_record_transition("Record Incomplete", "Record Complete") is True
    assert is_valid_record_transition("Deleted", "Record Complete") is False
    assert is_valid_record_transition("Custom Draft", "Custom Final") is True
    assert validate_record_transition("Custom Draft", "Custom Final") is True
    with pytest.raises(ValueError, match="Cannot transition record status"):
        validate_record_transition("Custom Draft", "Custom Draft")

    deleted_rec = Record.model_validate(
        {"studyKey": "S1", "recordId": 10, "deleted": True, "recordStatus": "Record Complete"}
    )
    assert deleted_rec.is_deleted is True
    assert deleted_rec.effective_status == RecordStatus.DELETED
    assert deleted_rec.can_transition_to(RecordStatus.LOCKED) is False

    with pytest.raises(ValueError, match="soft-deleted Record"):
        deleted_rec.transition_to(RecordStatus.LOCKED)
    with pytest.raises(ValueError, match="soft-deleted Record"):
        deleted_rec.update_status("Verified")
    with pytest.raises(ValueError, match="soft-deleted Record"):
        deleted_rec.record_status = "Record Complete"
    with pytest.raises(ValueError, match="Cannot restore a soft-deleted Record"):
        deleted_rec.deleted = False

    rec = Record.model_validate(
        {"studyKey": "S1", "recordId": 11, "deleted": False, "recordStatus": "Record Incomplete"}
    )
    assert rec.is_deleted is False
    assert rec.effective_status == RecordStatus.RECORD_INCOMPLETE
    assert rec.can_transition_to(RecordStatus.RECORD_COMPLETE) is True
    rec.transition_to(RecordStatus.RECORD_COMPLETE)
    assert rec.record_status == RecordStatus.RECORD_COMPLETE
    rec.update_status(RecordStatus.DELETED)
    assert rec.deleted is True
    assert rec.effective_status == RecordStatus.DELETED


def test_record_revision_bounds_and_monotonic_chain_validation() -> None:
    """Verify 21 CFR Part 11 bounds and monotonic audit chain rules on RecordRevision."""
    # Reject record_revision < 1
    with pytest.raises(ValueError, match="record_revision must be >= 1"):
        RecordRevision(recordId=1, recordRevision=0, dataRevision=0)
    with pytest.raises(ValueError, match="record_revision must be >= 1"):
        RecordRevision(recordId=1, recordRevision=-2, dataRevision=0)

    # Reject data_revision < 0
    with pytest.raises(ValueError, match="data_revision must be >= 0"):
        RecordRevision(recordId=1, recordRevision=1, dataRevision=-1)

    # Reject data_revision > record_revision
    with pytest.raises(ValueError, match="cannot exceed"):
        RecordRevision(recordId=1, recordRevision=2, dataRevision=3)

    # Reject booleans masquerading as ints
    with pytest.raises(ValueError):
        RecordRevision(recordId=1, recordRevision=True, dataRevision=0)  # type: ignore[arg-type]

    # Guard post-init attribute mutation
    r1 = RecordRevision(
        recordId=100,
        recordOid="REC-100",
        recordRevision=1,
        dataRevision=1,
        recordStatus="Record Incomplete",
    )
    assert r1.record_status == RecordStatus.RECORD_INCOMPLETE
    with pytest.raises(ValueError, match="cannot exceed"):
        r1.data_revision = 2
    with pytest.raises(ValueError, match="record_revision must be >= 1"):
        r1.record_revision = 0

    # Status-only revision increments record_revision while keeping data_revision constant
    r2 = RecordRevision(
        recordId=100,
        recordOid="REC-100",
        recordRevision=2,
        dataRevision=1,
        recordStatus="Record Complete",
    )
    r3 = RecordRevision(
        recordId=100,
        recordOid="REC-100",
        recordRevision=3,
        dataRevision=2,
        recordStatus="Verified",
    )

    assert r2.validate_follows(r1) is True
    assert r2.can_follow(r1) is True
    assert r1.can_follow(r2) is False
    assert len(RecordRevision.validate_chain([r1, r2, r3])) == 3
    assert len(validate_record_revision_chain([r1, r2, r3])) == 3
    assert is_valid_record_revision_chain([r1, r2, r3]) is True

    # Non-increasing record_revision fails
    dup_rev = RecordRevision(recordId=100, recordOid="REC-100", recordRevision=2, dataRevision=2)
    with pytest.raises(ValueError, match="Non-monotonic record_revision"):
        validate_record_revision_chain([r1, r2, dup_rev])
    assert is_valid_record_revision_chain([r1, r2, dup_rev]) is False

    # Decreasing data_revision fails
    regressed_data = RecordRevision(
        recordId=100, recordOid="REC-100", recordRevision=3, dataRevision=0
    )
    with pytest.raises(ValueError, match="Decreasing data_revision"):
        validate_record_revision_chain([r1, r2, regressed_data])
    assert is_valid_record_revision_chain([r1, r2, regressed_data]) is False

    # Separate records in a combined stream validate independently per record_id
    other_rec = RecordRevision(recordId=200, recordOid="REC-200", recordRevision=1, dataRevision=0)
    assert len(validate_record_revision_chain([r1, other_rec, r2, r3])) == 4

    # Missing record_revision in chain raises ValueError
    missing_rev = RecordRevision(recordId=300)
    with pytest.raises(ValueError, match="requires non-null record_revision"):
        validate_record_revision_chain([missing_rev])

    # Revision gap (1 -> 3) within the same study fails
    gap_rev = RecordRevision(
        studyKey="STUDY_A",
        recordId=100,
        recordOid="REC-100",
        recordRevision=3,
        dataRevision=2,
        dateCreated="2026-11-15 18:40:00",
    )
    r1_study_a = RecordRevision(
        studyKey="STUDY_A",
        recordId=100,
        recordOid="REC-100",
        recordRevision=1,
        dataRevision=1,
        dateCreated="2026-11-15 18:10:00",
    )
    with pytest.raises(ValueError, match="Missing record_revision in chain"):
        validate_record_revision_chain([r1_study_a, gap_rev])
    assert is_valid_record_revision_chain([r1_study_a, gap_rev]) is False

    # Backdated date_created within the same study fails
    backdated_r2 = RecordRevision(
        studyKey="STUDY_A",
        recordId=100,
        recordOid="REC-100",
        recordRevision=2,
        dataRevision=1,
        dateCreated="2026-11-15 18:05:00",
    )
    with pytest.raises(ValueError, match="Backdated date_created"):
        validate_record_revision_chain([r1_study_a, backdated_r2])
    assert is_valid_record_revision_chain([r1_study_a, backdated_r2]) is False

    # Shared recordId / recordOid across distinct Studies validate independently
    r2_study_a = RecordRevision(
        studyKey="STUDY_A",
        recordId=100,
        recordOid="REC-100",
        recordRevision=2,
        dataRevision=1,
        dateCreated="2026-11-15 18:20:00",
    )
    r1_study_b = RecordRevision(
        studyKey="STUDY_B",
        recordId=100,
        recordOid="REC-100",
        recordRevision=1,
        dataRevision=1,
        dateCreated="2026-11-15 18:00:00",
    )
    r2_study_b = RecordRevision(
        studyKey="STUDY_B",
        recordId=100,
        recordOid="REC-100",
        recordRevision=2,
        dataRevision=2,
        dateCreated="2026-11-15 18:15:00",
    )
    cross_study = validate_record_revision_chain([r1_study_a, r1_study_b, r2_study_a, r2_study_b])
    assert len(cross_study) == 4
    assert is_valid_record_revision_chain([r1_study_a, r1_study_b, r2_study_a, r2_study_b]) is True

    # Verify fake_record_revision always satisfies bounds invariants
    for _ in range(50):
        sample = RecordRevision.model_validate(fake_record_revision())
        assert sample.record_revision is not None and sample.record_revision >= 1
        assert (
            sample.data_revision is not None and 0 <= sample.data_revision <= sample.record_revision
        )


def test_fake_record_with_study_scoped_schema_cache() -> None:
    """Verify fake_record works with study-partitioned SchemaCache."""
    from imednet.models.variables import Variable

    cache = SchemaCache()
    cache.populate(
        [
            Variable(
                studyKey="STUDY_A",
                variableName="AGE",
                variableType="integer",
                formId=10,
                formKey="DM",
            )
        ],
        study_key="STUDY_A",
    )
    rec = fake_record(schema=cache)
    assert rec["formKey"] == "DM"
    assert "AGE" in rec["recordData"]


def test_strict_and_partial_slice_record_revision_chain_invariants() -> None:
    """Verify strict initial revision == 1, irreversible deletion, and allow_partial=True slice validation."""
    from unittest.mock import MagicMock

    from imednet_workflows.data_extraction import DataExtractionWorkflow

    r1 = RecordRevision(
        studyKey="STUDY_AUDIT",
        recordRevisionId=1,
        recordId=500,
        recordOid="REC-500",
        recordRevision=1,
        dataRevision=1,
        recordStatus="Record Incomplete",
        user="crc_user",
        deleted=False,
        dateCreated="2026-01-10T09:00:00Z",
    )
    r2 = RecordRevision(
        studyKey="STUDY_AUDIT",
        recordRevisionId=2,
        recordId=500,
        recordOid="REC-500",
        recordRevision=2,
        dataRevision=2,
        recordStatus="Record Complete",
        user="pi_user",
        deleted=False,
        dateCreated="2026-01-12T10:00:00Z",
    )
    r3_deleted = RecordRevision(
        studyKey="STUDY_AUDIT",
        recordRevisionId=3,
        recordId=500,
        recordOid="REC-500",
        recordRevision=3,
        dataRevision=2,
        recordStatus="Record Complete",
        user="crc_user",
        deleted=True,
        dateCreated="2026-01-15T11:00:00Z",
    )
    r4_still_deleted = RecordRevision(
        studyKey="STUDY_AUDIT",
        recordRevisionId=4,
        recordId=500,
        recordOid="REC-500",
        recordRevision=4,
        dataRevision=2,
        recordStatus="Locked",
        user="pi_user",
        deleted=True,
        dateCreated="2026-01-18T12:00:00Z",
    )
    r4_resurrected = RecordRevision(
        studyKey="STUDY_AUDIT",
        recordRevisionId=5,
        recordId=500,
        recordOid="REC-500",
        recordRevision=4,
        dataRevision=2,
        recordStatus="Record Complete",
        user="pi_user",
        deleted=False,
        dateCreated="2026-01-18T12:00:00Z",
    )

    # 1. Strict mode rejects chains not starting at record_revision == 1
    with pytest.raises(ValueError, match="Missing initial record_revision in chain: expected 1"):
        validate_record_revision_chain([r2, r3_deleted])
    assert is_valid_record_revision_chain([r2, r3_deleted]) is False

    # 2. Irreversible deletion: deleted=True -> deleted=True passes; deleted=True -> deleted=False fails
    assert len(validate_record_revision_chain([r1, r2, r3_deleted, r4_still_deleted])) == 4
    with pytest.raises(ValueError, match="Irreversible deletion violated"):
        validate_record_revision_chain([r1, r2, r3_deleted, r4_resurrected])
    assert is_valid_record_revision_chain([r1, r2, r3_deleted, r4_resurrected]) is False

    # Irreversible deletion is also enforced in partial-slice mode
    with pytest.raises(ValueError, match="Irreversible deletion violated"):
        validate_record_revision_chain([r3_deleted, r4_resurrected], allow_partial=True)
    assert is_valid_record_revision_chain([r3_deleted, r4_resurrected], allow_partial=True) is False

    # 3. Actor-filtered slice from DataExtractionWorkflow.extract_audit_trail (revs 2 and 4)
    mock_sdk = MagicMock()
    mock_sdk.get_record_revisions.return_value = [r2, r4_still_deleted]
    workflow = DataExtractionWorkflow(mock_sdk)
    actor_slice = workflow.extract_audit_trail(
        "STUDY_AUDIT",
        start_date="2026-01-11",
        end_date="2026-01-20",
        user="pi_user",
        sort="recordRevision,asc",
    )
    assert [rev.record_revision for rev in actor_slice] == [2, 4]
    with pytest.raises(ValueError):
        RecordRevision.validate_chain(actor_slice)
    validated_slice = RecordRevision.validate_chain(actor_slice, allow_partial=True)
    assert [rev.record_revision for rev in validated_slice] == [2, 4]
    assert is_valid_record_revision_chain(actor_slice, allow_partial=True) is True

    # 4. Partial-slice mode still rejects out-of-order revisions, decreasing data_revision, and backdated timestamps
    with pytest.raises(ValueError, match="Non-monotonic record_revision"):
        RecordRevision.validate_chain([r4_still_deleted, r2], allow_partial=True)
    r5_regressed_data = RecordRevision(
        studyKey="STUDY_AUDIT",
        recordId=500,
        recordRevision=5,
        dataRevision=1,
        deleted=True,
        dateCreated="2026-01-20T12:00:00Z",
    )
    with pytest.raises(ValueError, match="Decreasing data_revision"):
        RecordRevision.validate_chain([r4_still_deleted, r5_regressed_data], allow_partial=True)
    r5_backdated = RecordRevision(
        studyKey="STUDY_AUDIT",
        recordId=500,
        recordRevision=5,
        dataRevision=2,
        deleted=True,
        dateCreated="2026-01-16T00:00:00Z",
    )
    with pytest.raises(ValueError, match="Backdated date_created"):
        RecordRevision.validate_chain([r4_still_deleted, r5_backdated], allow_partial=True)
