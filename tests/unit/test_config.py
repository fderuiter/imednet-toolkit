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


def test_load_config_does_not_mutate_strict_mode_env(monkeypatch):
    """load_config resolves strict_mode without mutating os.environ."""
    import os

    monkeypatch.delenv("IMEDNET_STRICT_MODE", raising=False)
    cfg = load_config(api_key="key", security_key="sec", strict_mode=True)
    assert cfg.strict_mode is True
    assert "IMEDNET_STRICT_MODE" not in os.environ

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "false")
    cfg2 = load_config(api_key="key", security_key="sec", strict_mode=True)
    assert cfg2.strict_mode is True
    assert os.environ["IMEDNET_STRICT_MODE"] == "false"


def test_merge_config_overrides_preserves_and_overrides_without_env(monkeypatch):
    """merge_config_overrides preserves fields, applies explicit overrides, and ignores env."""
    from imednet.auth import Credentials, MemoryCredentialStore
    from imednet.config import merge_config_overrides

    monkeypatch.setenv("IMEDNET_API_KEY", "env_ignored")
    monkeypatch.setenv("IMEDNET_TIMEOUT", "999")
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "true")

    base = Config(
        api_key="base_key",  # pragma: allowlist secret
        security_key="base_sec",  # pragma: allowlist secret
        base_url="https://base.imednet.com",
        oidc_token="base_oidc",
        timeout=45.0,
        strict_mode=False,
        simulation_mode=False,
    )
    same = merge_config_overrides(base)
    assert same is base

    overridden = merge_config_overrides(base, timeout=12.5, strict_mode=True)
    assert overridden.timeout == 12.5
    assert overridden.strict_mode is True
    assert overridden.oidc_token == "base_oidc"
    assert overridden.api_key == "base_key"  # pragma: allowlist secret

    store = MemoryCredentialStore(Credentials(oidc_token="source_oidc"))
    from_source = merge_config_overrides(
        Config(base_url="https://source.imednet.com"),
        credential_source=store,
    )
    assert from_source.oidc_token == "source_oidc"

    sim = merge_config_overrides(Config(simulation_mode=True))
    assert sim.simulation_mode is True
    assert sim.api_key == "simulated_api_key"  # pragma: allowlist secret
    assert sim.security_key == "simulated_security_key"  # pragma: allowlist secret

    with pytest.raises(ValueError, match="Either OIDC token or both API key and security key"):
        merge_config_overrides(Config())
