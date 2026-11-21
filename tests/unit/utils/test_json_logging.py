"""Unit tests for json logging."""

import importlib
import json
import logging
import sys
import types

import pytest

MODULE_PATH = "imednet.utils.json_logging"


def _install_formatter(monkeypatch: pytest.MonkeyPatch, submodule: str) -> type[logging.Formatter]:
    """Insert a dummy JsonFormatter under the given pythonjsonlogger submodule."""
    for name in (
        "pythonjsonlogger",
        "pythonjsonlogger.json",
        "pythonjsonlogger.jsonlogger",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    package = types.ModuleType("pythonjsonlogger")
    package.__path__ = []  # mark as package so missing submodules raise ModuleNotFoundError
    monkeypatch.setitem(sys.modules, "pythonjsonlogger", package)

    module = types.ModuleType(f"pythonjsonlogger.{submodule}")

    class DummyFormatter(logging.Formatter):
        """Test suite for DummyFormatter."""

        def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
            """Helper function to format."""
            return json.dumps({"message": record.getMessage(), "levelname": record.levelname})

    module.JsonFormatter = DummyFormatter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"pythonjsonlogger.{submodule}", module)
    return DummyFormatter


def _configure_and_log(json_logging, caplog: pytest.LogCaptureFixture) -> logging.Handler:
    """Helper function to  configure and log."""
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    with caplog.at_level(logging.INFO, logger=""):
        json_logging.configure_json_logging()
        logging.getLogger().addHandler(caplog.handler)
        logging.getLogger().info("hello")
        record = caplog.records[0]
    handler = logging.getLogger().handlers[0]
    formatted = handler.format(record)
    data = json.loads(formatted)
    assert data == {"message": "hello", "levelname": "INFO"}
    root.handlers = old_handlers
    root.setLevel(old_level)
    return handler


def test_configure_json_logging_uses_json_import(monkeypatch, caplog):
    """Test that configure json logging uses json import."""
    formatter_cls = _install_formatter(monkeypatch, "json")
    monkeypatch.delitem(sys.modules, MODULE_PATH, raising=False)
    json_logging = importlib.import_module(MODULE_PATH)
    handler = _configure_and_log(json_logging, caplog)
    assert isinstance(handler.formatter, formatter_cls)


def test_configure_json_logging_uses_jsonlogger_import(monkeypatch, caplog):
    """Test that configure json logging uses jsonlogger import."""
    formatter_cls = _install_formatter(monkeypatch, "jsonlogger")
    monkeypatch.delitem(sys.modules, MODULE_PATH, raising=False)
    json_logging = importlib.import_module(MODULE_PATH)
    handler = _configure_and_log(json_logging, caplog)
    assert isinstance(handler.formatter, formatter_cls)


def test_configure_json_logging_falls_back_when_dependency_missing(monkeypatch, capsys):
    """Test fallback StreamHandler configuration when python-json-logger is absent."""
    for name in (
        "pythonjsonlogger",
        "pythonjsonlogger.json",
        "pythonjsonlogger.jsonlogger",
    ):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, MODULE_PATH, raising=False)
    json_logging = importlib.import_module(MODULE_PATH)

    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        json_logging.configure_json_logging(level=logging.WARNING)
        captured = capsys.readouterr()
        assert "python-json-logger not installed" in captured.err
        assert root.level == logging.WARNING
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)
