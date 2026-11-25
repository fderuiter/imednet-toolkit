"""Unit tests for strict and partial-slice 21 CFR Part 11 RecordRevision chain invariants."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from imednet.models.record_revisions import (
    RecordRevision,
    is_valid_record_revision_chain,
    validate_record_revision_chain,
)
from imednet_workflows.data_extraction import DataExtractionWorkflow


def test_strict_and_partial_slice_record_revision_chain_invariants() -> None:
    """Verify strict initial revision == 1, irreversible deletion, and allow_partial=True slice validation."""
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
    assert r4_resurrected.can_follow(r3_deleted) is False
    assert r4_resurrected.can_follow(r3_deleted, allow_partial=True) is False
    assert r4_still_deleted.can_follow(r2, allow_partial=True) is True
    assert r4_still_deleted.can_follow(r2, allow_partial=False) is False

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
