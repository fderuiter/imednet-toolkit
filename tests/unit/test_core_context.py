"""Unit tests for core context."""

import pytest

from imednet.core.context import (
    clear_all_contexts,
    clear_base_url_context,
    clear_security_key_context,
    clear_strict_mode_context,
    get_base_url_context,
    get_current_study,
    get_security_key_context,
    get_study_context,
    is_strict_mode,
    reset_study_context,
    set_base_url_context,
    set_security_key_context,
    set_strict_mode_context,
    set_study_context,
    study_context,
)
from imednet.errors.validation import ConfigurationError


def test_set_and_reset_study_context() -> None:
    """Test that set and reset study context."""
    token = set_study_context("S1")
    assert get_current_study() == "S1"
    reset_study_context(token)
    with pytest.raises(ConfigurationError):
        get_current_study()


def test_study_context_manager_resets_to_none() -> None:
    """Test that study context manager resets to none."""
    with study_context("S1"):
        assert get_current_study() == "S1"
        assert get_study_context() == "S1"

    assert get_study_context() is None


def test_study_context_manager_restores_previous_context() -> None:
    """Test that study context manager restores previous context."""
    with study_context("OUTER"):
        with study_context("INNER"):
            assert get_current_study() == "INNER"
        assert get_current_study() == "OUTER"


def test_clear_individual_and_all_contexts() -> None:
    """Verify clear_* helpers reset each ContextVar deterministically."""
    set_base_url_context("https://ctx.example.test")
    set_security_key_context("sec-token-1")  # pragma: allowlist secret
    set_strict_mode_context(True)
    assert get_base_url_context() == "https://ctx.example.test"
    assert get_security_key_context() == "sec-token-1"  # pragma: allowlist secret
    assert is_strict_mode() is True

    clear_base_url_context()
    assert get_base_url_context() is None
    clear_security_key_context()
    assert get_security_key_context() is None
    clear_strict_mode_context()
    assert is_strict_mode() is False

    set_study_context("S-CLEAR")
    set_base_url_context("https://ctx2.example.test")
    set_security_key_context("sec-token-2")  # pragma: allowlist secret
    set_strict_mode_context(True)

    clear_all_contexts()
    assert get_study_context() is None
    assert get_base_url_context() is None
    assert get_security_key_context() is None
    assert is_strict_mode() is False
