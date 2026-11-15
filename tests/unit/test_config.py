"""Unit tests for config."""

import pytest

from imednet.config import Config, load_config


def test_load_config_from_env(monkeypatch):
    """Test that load config from env."""
    monkeypatch.setenv("IMEDNET_API_KEY", "env_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "env_secret")
    monkeypatch.setenv("IMEDNET_BASE_URL", " https://example.com ")

    cfg = load_config()
    assert cfg == Config(
        api_key="env_key", security_key="env_secret", base_url="https://example.com"
    )


def test_load_config_overrides_env(monkeypatch):
    """Test that load config overrides env."""
    monkeypatch.setenv("IMEDNET_API_KEY", "env_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "env_secret")
    cfg = load_config(api_key="arg_key", security_key="arg_sec", base_url="https://override")
    assert cfg == Config(api_key="arg_key", security_key="arg_sec", base_url="https://override")


def test_load_config_missing(monkeypatch):
    """Test that load config missing."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    with pytest.raises(ValueError, match="API key and security key are required"):
        load_config()


@pytest.mark.parametrize(
    ("api_key", "security_key", "expected_error"),
    [
        ("   ", "valid", "API key is required"),
        ("valid", "   ", "Security key is required"),
        ("   ", "   ", "API key and security key are required"),
        ("", "valid", "API key is required"),
        ("valid", "", "Security key is required"),
    ],
)
def test_load_config_whitespace_args(monkeypatch, api_key, security_key, expected_error):
    """Test that whitespace/empty arguments raise ValueError."""
    # Ensure no environment variables interfere
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)

    with pytest.raises(ValueError, match=expected_error):
        load_config(api_key=api_key, security_key=security_key)


@pytest.mark.parametrize(
    ("env_api_key", "env_security_key", "expected_error"),
    [
        ("   ", "valid", "API key is required"),
        ("valid", "   ", "Security key is required"),
    ],
)
def test_load_config_whitespace_env(monkeypatch, env_api_key, env_security_key, expected_error):
    """Test that whitespace environment variables raise ValueError."""
    monkeypatch.setenv("IMEDNET_API_KEY", env_api_key)
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", env_security_key)
    with pytest.raises(ValueError, match=expected_error):
        load_config()


def test_config_rejects_obsolete_a11y_fields():
    """Test that Config and load_config reject removed accessibility fields."""
    cfg = Config(api_key="key", security_key="sec")
    assert not hasattr(cfg, "vpat_path")
    assert not hasattr(cfg, "a11y_report_path")

    with pytest.raises(TypeError):
        Config(api_key="key", security_key="sec", vpat_path="/custom/vpat.md")  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        Config(api_key="key", security_key="sec", a11y_report_path="/custom/a11y.json")  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        load_config(api_key="key", security_key="sec", vpat_path="/custom/vpat.md")  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        load_config(api_key="key", security_key="sec", a11y_report_path="/custom/a11y.json")  # type: ignore[call-arg]


def test_load_config_ignores_obsolete_env_vars(monkeypatch):
    """Test that load_config ignores obsolete accessibility environment variables."""
    monkeypatch.setenv("IMEDNET_API_KEY", "env_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "env_secret")
    monkeypatch.setenv("IMEDNET_VPAT_PATH", "/custom/vpat.md")
    monkeypatch.setenv("IMEDNET_A11Y_REPORT_PATH", "/custom/a11y.json")

    cfg = load_config()
    assert not hasattr(cfg, "vpat_path")
    assert not hasattr(cfg, "a11y_report_path")
