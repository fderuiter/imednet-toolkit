"""Unit tests for credential redaction."""

import logging
from unittest.mock import MagicMock

import pytest
import respx


class Result:
    def __init__(self, exit_code, stdout, stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.output = stdout + stderr


class CliRunner:
    def invoke(self, app, args):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                if hasattr(app, "parse_args"):
                    pass
                app(args)
        except SystemExit as e:
            exit_code = e.code or 0
        except Exception:
            import traceback

            err.write(traceback.format_exc())
            exit_code = 1

        # We also need to catch argparse sys.exit(2)
        return Result(exit_code, out.getvalue(), err.getvalue())


from imednet.auth.api_key import ApiKeyAuth
from imednet.core.client import Client
from imednet.errors import ApiError, AuthenticationError, RateLimitError


def test_api_key_auth_repr_and_str_mask_secrets() -> None:
    """Test that api key auth repr and str mask secrets."""
    auth = ApiKeyAuth(api_key="plain-api-key", security_key="plain-security-key")

    assert "plain-api-key" not in repr(auth)
    assert "plain-security-key" not in repr(auth)
    assert "plain-api-key" not in str(auth)
    assert "plain-security-key" not in str(auth)
    assert "********" in repr(auth)


@pytest.mark.parametrize("error_cls", [AuthenticationError, RateLimitError])
def test_api_errors_mask_sensitive_values(error_cls: type[ApiError]) -> None:
    """Test that api errors mask sensitive values."""
    secret_api_key = "very-secret-api-key"
    secret_token = "very-secret-token"
    secret_auth = "Bearer very-secret-authorization"
    error = error_cls(
        {
            "api_key": secret_api_key,
            "token": secret_token,
            "Authorization": secret_auth,
            "detail": f"Authorization: {secret_auth}",
            "nested": {"x-imn-security-key": "very-secret-security-key"},
        },
        status_code=401,
    )

    text = str(error)
    assert secret_api_key not in text
    assert secret_token not in text
    assert secret_auth not in text
    assert "x-imn-security-key" in text
    assert "***" in text


def test_http_client_never_logs_authorization_header(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that http client never logs authorization header."""
    monkeypatch.setenv("IMEDNET_API_KEY", "api")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "security")
    caplog.set_level("DEBUG")
    caplog.set_level("DEBUG", logger="httpx")
    caplog.set_level("DEBUG", logger="httpcore")
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level

    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        router.get("https://api.test/secure").respond(200, json={"ok": True})
        client = Client(api_key="api", security_key="security", base_url="https://api.test")
        client.get("/secure", headers={"Authorization": "Bearer leaked-token"})

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Authorization" not in messages
    assert "leaked-token" not in messages
    assert httpx_logger.level == original_httpx_level
    assert httpcore_logger.level == original_httpcore_level


def test_cli_surfaces_redacted_authentication_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that cli surfaces redacted authentication errors."""
    from importlib import import_module

    runner = CliRunner()
    monkeypatch.setenv("IMEDNET_API_KEY", "api")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "security")

    sdk = MagicMock()
    sdk.studies.list.side_effect = AuthenticationError(
        {
            "api_key": "very-secret-api-key",
            "Authorization": "Bearer very-secret-authorization",
        },
        status_code=401,
    )

    # Use the active modules registered in sys.modules
    context_module = import_module("imednet.cli.utils.context")
    monkeypatch.setattr(context_module, "get_sdk", MagicMock(return_value=sdk))

    cli_module = import_module("imednet.cli")
    result = runner.invoke(cli_module.app, ["studies", "list"])

    assert result.exit_code == 1
    assert "Authentication Failed" in result.stderr
    assert "very-secret-api-key" not in result.stderr
    assert "very-secret-authorization" not in result.stderr
    assert "very-secret-api-key" not in result.stdout
    assert "very-secret-authorization" not in result.stdout
