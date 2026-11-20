"""Unit tests for decorators."""

from unittest.mock import MagicMock

import pytest


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


from imednet import cli


def test_decorator_handles_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that decorator handles unexpected error."""
    runner = CliRunner()
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")
    sdk = MagicMock()
    sdk.studies.list.side_effect = RuntimeError("boom")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=sdk))

    result = runner.invoke(cli.app, ["studies", "list"])

    assert result.exit_code == 1
    assert "Unexpected error" in result.stderr
    assert "Unexpected error" not in result.stdout


def test_cli_execution_guard_clears_line_and_exits_130(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cli_execution_guard clears active status line, prints Aborted by user., and exits 130."""
    from unittest.mock import patch

    from imednet.spi.cli import cli_execution_guard

    with (
        patch("imednet.cli.utils.output.clear_line") as mock_clear,
        pytest.raises(SystemExit) as exc_info,
        cli_execution_guard(),
    ):
        raise KeyboardInterrupt()

    assert exc_info.value.code == 130
    mock_clear.assert_called_once()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Aborted by user." in captured.err


def test_positive_int_validator() -> None:
    """positive_int accepts strictly positive integers and rejects <= 0 or non-integers."""
    import argparse

    from imednet.spi.cli import positive_int

    assert positive_int("1") == 1
    assert positive_int("900") == 900

    for invalid in ("0", "-1", "abc", "1.5", True, False):
        with pytest.raises(argparse.ArgumentTypeError):
            positive_int(invalid)  # type: ignore[arg-type]


def test_get_sdk_redacts_secrets_on_init_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """get_sdk redacts environment secrets if SDK initialization fails unexpectedly."""
    from unittest.mock import patch

    from imednet.cli.utils.context import get_sdk

    oidc_val = "oidc_secret_init_token_777"  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_OIDC_TOKEN", oidc_val)
    with (
        patch("imednet.cli.utils.context.load_config", return_value=MagicMock()),
        patch(
            "imednet.cli.utils.context.ImednetSDK",
            side_effect=RuntimeError(f"bad token {oidc_val}"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        get_sdk()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert oidc_val not in err
    assert "********" in err
