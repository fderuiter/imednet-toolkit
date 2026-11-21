"""JSON logging configuration utility."""

import logging
import sys


def configure_json_logging(level: int = logging.INFO) -> None:
    """Configure root logger to emit JSON formatted logs."""
    handler = logging.StreamHandler()
    try:  # python-json-logger >= 3.0
        from pythonjsonlogger.json import JsonFormatter
    except ModuleNotFoundError:  # pragma: no cover - fallback for older versions
        try:
            from pythonjsonlogger.jsonlogger import JsonFormatter
        except ModuleNotFoundError:
            print(
                "Warning: python-json-logger not installed. Install with `pip install imednet[cli]` for JSON logging.",
                file=sys.stderr,
            )
            logging.basicConfig(level=level, handlers=[handler], force=True)
            return

    formatter = JsonFormatter()
    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler], force=True)
