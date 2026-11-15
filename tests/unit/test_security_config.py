"""Unit tests for security config."""

from imednet.config import Config


def test_config_repr_masks_secrets():
    """Test that Config.__repr__ masks sensitive information."""
    api_key = "secret_api_key_123"
    security_key = "secret_security_key_456"
    base_url = "https://example.com"

    config = Config(api_key=api_key, security_key=security_key, base_url=base_url)

    repr_str = repr(config)

    # Assert secrets are NOT in the repr
    assert api_key not in repr_str
    assert security_key not in repr_str

    # Assert masked values are present
    assert "********" in repr_str or "***" in repr_str

    # Assert non-sensitive info is present
    assert base_url in repr_str

    # Assert obsolete accessibility fields are NOT in repr or attributes
    assert "vpat_path" not in repr_str
    assert "a11y_report_path" not in repr_str
    assert not hasattr(config, "vpat_path")
    assert not hasattr(config, "a11y_report_path")
