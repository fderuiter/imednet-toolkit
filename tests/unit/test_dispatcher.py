"""Unit tests for the export dispatcher and registry."""

from unittest.mock import MagicMock, patch

import pytest

from imednet.integrations.dispatcher import export, register_sink_target, register_tabular_target
from imednet.integrations.sink_base import ExportSink


class DummySink(ExportSink):
    """A dummy sink for testing routing and lifecycle management."""

    def __init__(self, config=None, **kwargs):
        """Initialize the dummy sink."""
        super().__init__(config)
        self.kwargs = kwargs
        self.written_batches = 0
        self.flushed = False
        self.closed = False

    def write_batch(self, records, *, batch_id):
        """Mock write_batch."""
        self.written_batches += len(records)
        return len(records)

    def flush(self):
        """Mock flush."""
        self.flushed = True

    def close(self):
        """Mock close."""
        self.closed = True


def dummy_tabular(sdk, study_key, **kwargs):
    """A dummy tabular function for testing."""
    return {"tabular": True, "sdk": sdk, "study_key": study_key, "kwargs": kwargs}


def test_export_tabular_routing():
    """Test that export correctly routes to a registered tabular function."""
    register_tabular_target("dummy_tab", dummy_tabular)

    sdk_mock = MagicMock()
    result = export("dummy_tab", sdk_mock, "STUDY1", my_arg="value")

    assert result["tabular"] is True
    assert result["sdk"] is sdk_mock
    assert result["study_key"] == "STUDY1"
    assert result["kwargs"] == {"my_arg": "value"}


@patch("imednet.integrations.dispatcher.iter_batches")
@patch("imednet.integrations.dispatcher.apply_quality_gate")
def test_export_sink_routing(mock_quality_gate, mock_iter_batches):
    """Test that export correctly routes to a registered sink class and handles lifecycle."""
    register_sink_target("dummy_sink", DummySink)

    sdk_mock = MagicMock()
    # Mock records return
    sdk_mock.records.list.return_value = [{"id": 1}, {"id": 2}]
    mock_quality_gate.return_value = [{"id": 1}, {"id": 2}]
    mock_iter_batches.return_value = [[{"id": 1}], [{"id": 2}]]

    total = export("dummy_sink", sdk_mock, "STUDY2", extra_arg="test")

    assert total == 2
    mock_quality_gate.assert_called_once()
    mock_iter_batches.assert_called_once()


def test_export_unsupported_target():
    """Test that an unknown target raises a ValueError."""
    sdk_mock = MagicMock()
    with pytest.raises(ValueError, match="Unsupported export target: 'unknown_target'"):
        export("unknown_target", sdk_mock, "STUDY3")


class MockEntryPoint:
    """Mock EntryPoint for testing dynamic discovery."""

    def __init__(self, name, load_func):
        self.name = name
        self.value = f"mock.{name}"
        self.load = load_func


def test_get_sink_dynamic_discovery_success(monkeypatch):
    from imednet.integrations.dispatcher import ExportRegistry

    registry = ExportRegistry()

    class DummyClass:
        pass

    ep = MockEntryPoint("dummy_target", lambda: DummyClass)
    monkeypatch.setattr(
        "imednet.integrations.dispatcher.entry_points",
        lambda *, group: [ep] if group == "imednet.sinks" else [],
    )

    sink = registry.get_sink("dummy_target")
    assert sink is DummyClass
    assert registry._sink_targets["dummy_target"] is DummyClass


def test_get_sink_dynamic_discovery_import_error(monkeypatch):
    from imednet.integrations.dispatcher import ExportRegistry

    registry = ExportRegistry()

    def raise_import():
        raise ImportError("Optional dep missing")

    ep = MockEntryPoint("dummy_target", raise_import)
    monkeypatch.setattr(
        "imednet.integrations.dispatcher.entry_points",
        lambda *, group: [ep] if group == "imednet.sinks" else [],
    )

    sink = registry.get_sink("dummy_target")
    assert sink is None


def test_get_sink_dynamic_discovery_attribute_error(monkeypatch):
    from imednet.integrations.dispatcher import ExportRegistry

    registry = ExportRegistry()

    def raise_attr():
        raise AttributeError("Symbol not found")

    ep = MockEntryPoint("dummy_target", raise_attr)
    monkeypatch.setattr(
        "imednet.integrations.dispatcher.entry_points",
        lambda *, group: [ep] if group == "imednet.sinks" else [],
    )

    sink = registry.get_sink("dummy_target")
    assert sink is None


def test_get_config_class_dynamic_discovery_success(monkeypatch):
    from imednet.integrations.dispatcher import ExportRegistry

    registry = ExportRegistry()

    class DummyConfigClass:
        pass

    ep = MockEntryPoint("dummy_target", lambda: DummyConfigClass)
    monkeypatch.setattr(
        "imednet.integrations.dispatcher.entry_points",
        lambda *, group: [ep] if group == "imednet.sink_configs" else [],
    )

    config = registry.get_config_class("dummy_target")
    assert config is DummyConfigClass
    assert registry._config_targets["dummy_target"] is DummyConfigClass


def test_get_config_class_dynamic_discovery_import_error(monkeypatch):
    from imednet.integrations.dispatcher import ExportRegistry, SinkConfig

    registry = ExportRegistry()

    def raise_import():
        raise ImportError("Optional dep missing")

    ep = MockEntryPoint("dummy_target", raise_import)
    monkeypatch.setattr(
        "imednet.integrations.dispatcher.entry_points",
        lambda *, group: [ep] if group == "imednet.sink_configs" else [],
    )

    config = registry.get_config_class("dummy_target")
    assert config is SinkConfig


def test_get_config_class_dynamic_discovery_attribute_error(monkeypatch):
    from imednet.integrations.dispatcher import ExportRegistry, SinkConfig

    registry = ExportRegistry()

    def raise_attr():
        raise AttributeError("Symbol not found")

    ep = MockEntryPoint("dummy_target", raise_attr)
    monkeypatch.setattr(
        "imednet.integrations.dispatcher.entry_points",
        lambda *, group: [ep] if group == "imednet.sink_configs" else [],
    )

    config = registry.get_config_class("dummy_target")
    assert config is SinkConfig


@patch("imednet.integrations.dispatcher.iter_batches")
@patch("imednet.integrations.dispatcher.apply_quality_gate")
def test_export_sink_routing_with_explicit_config(mock_quality_gate, mock_iter_batches):
    from imednet.integrations.dispatcher import SinkConfig, export, register_sink_target

    register_sink_target("dummy_sink", DummySink)

    sdk_mock = MagicMock()
    sdk_mock.records.list.return_value = [{"id": 1}]
    mock_quality_gate.return_value = [{"id": 1}]
    mock_iter_batches.return_value = [[{"id": 1}]]

    cfg = SinkConfig(study_key="STUDY_CFG")
    total = export("dummy_sink", sdk_mock, "STUDY_CFG", config=cfg)

    assert total == 1
