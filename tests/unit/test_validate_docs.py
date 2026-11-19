"""Unit tests for scripts/validate_docs.py API reference and backlink verification."""

from pathlib import Path

from scripts.validate_docs import check_no_duplicate_suppression, check_rendered_api_reference


def test_check_no_duplicate_suppression_detects_forbidden_patterns(tmp_path: Path) -> None:
    """check_no_duplicate_suppression flags _SuppressDuplicateDescriptions and duplicate filters."""
    clean_conf = tmp_path / "conf.py"
    clean_conf.write_text('project = "imednet"\n', encoding="utf-8")
    assert check_no_duplicate_suppression(clean_conf) is False

    suppressed_conf = tmp_path / "bad_conf.py"
    suppressed_conf.write_text("class _SuppressDuplicateDescriptions: pass\n", encoding="utf-8")
    assert check_no_duplicate_suppression(suppressed_conf) is True

    filtered_conf = tmp_path / "filtered_conf.py"
    filtered_conf.write_text(
        'warnings.filterwarnings("ignore", message="duplicate object description*")\n',
        encoding="utf-8",
    )
    assert check_no_duplicate_suppression(filtered_conf) is True


def test_check_rendered_api_reference_flags_builtins_and_broken_backlinks(tmp_path: Path) -> None:
    """check_rendered_api_reference fails when pages are missing or contain broken [docs] anchors."""
    assert check_rendered_api_reference(tmp_path) is True
