Plugin Authoring Guide
======================

The iMednet SDK monorepo contains the following first-party packages:

* :doc:`imednet </reference/api/core>` (Core SDK) — The foundational API client and SDK.
* :doc:`imednet-workflows </reference/api/plugins-workflows>` — Opinionated data-extraction and query-management workflows.
* :doc:`imednet-sinks </reference/api/plugins-sinks>` — Export sinks for data destinations.
* :doc:`imednet-tui </reference/api/plugins-tui>` — Interactive terminal user interface (TUI) plugin.
* :doc:`apache-airflow-providers-imednet </reference/api/providers-airflow>` — Apache Airflow integrations.

The built-in ``imednet-workflows`` package is the reference implementation for
third-party SDK plugins, while ``imednet-tui`` demonstrates CLI subcommand
registration through the ``imednet.cli_plugins`` entry-point group:

.. code-block:: toml

    [project.entry-points."imednet.cli_plugins"]
    tui = "imednet_tui.cli:setup_parser"

Any third-party package can register its own SDK or CLI plugin using the
standard `Python entry-point`_ mechanism.

.. _Python entry-point: https://packaging.python.org/en/latest/specifications/entry-points/

Optional-plugin CLI graceful-degradation verification
-----------------------------------------------------

CLI commands that depend on optional plugins should be tested in two modes:

* **Plugin present**: patch ``importlib.util.find_spec`` (or module import) to
  return a module and assert the command runs.
* **Plugin absent**: patch discovery to return ``None`` (or raise
  ``ImportError``) and assert the command exits with code ``1`` and prints
  install guidance.

See ``tests/unit/cli/test_cli.py`` and ``tests/unit/cli/test_cli_export.py``
for repository examples of this pattern.

Plugin authoring reference
--------------------------

How it works
------------

The SDK supports typed synchronous and asynchronous Python entry-point groups
for extending functionality on :class:`~imednet.sdk.ImednetSDK` and
:class:`~imednet.sdk.AsyncImednetSDK`:

* **Workflows (``imednet.workflows`` and ``imednet.async_workflows``)** —
  ``sdk.workflows`` is a :class:`~imednet.sdk.WorkflowRegistry` that resolves
  synchronous adapters from ``imednet.workflows`` on ``ImednetSDK`` and native
  asynchronous adapters from ``imednet.async_workflows`` on ``AsyncImednetSDK``
  (falling back to ``imednet.workflows`` entries that declare
  ``supports_async = True``). Each entry point is loaded and instantiated lazily
  on first attribute access by passing the active SDK facade.
* **Export sinks (``imednet.sinks`` and ``imednet.async_sinks``)** —
  :func:`~imednet.integrations.dispatcher.export` discovers
  :class:`~imednet.spi.sinks.ExportSink` classes in ``imednet.sinks``, while
  :func:`~imednet.integrations.dispatcher.async_export` discovers
  :class:`~imednet.spi.sinks.AsyncExportSink` classes in ``imednet.async_sinks``.
* **Namespace plugins (``imednet.plugins``)** — Top-level SDK namespaces such
  as ``sdk.sinks`` query the ``imednet.plugins`` entry-point group (for
  example, ``sinks = "imednet_sinks.plugin:create_sinks"``) and invoke the
  registered factory with the SDK instance to return a mode-matched namespace
  (:class:`~imednet.plugins.SinksNamespaceProtocol` on ``ImednetSDK`` and
  :class:`~imednet.plugins.AsyncSinksNamespaceProtocol` on ``AsyncImednetSDK``).

If a workflow or plugin package is **not** installed, accessing the missing
workflow or namespace attribute raises :exc:`AttributeError` or
:exc:`ImportError` with installation guidance.

If a namespace plugin **is** installed under ``imednet.plugins`` but fails to
load (duplicate registration, broken import, or non-callable target), a
:exc:`~imednet.errors.PluginLoadError` is raised when loading the namespace.

Plugin contract
---------------

Workflow adapters registered under ``imednet.workflows`` accept
:class:`~imednet.spi.facade.ImednetFacade`, while asynchronous adapters
registered under ``imednet.async_workflows`` accept
:class:`~imednet.spi.facade.AsyncImednetFacade` and expose symmetric unprefixed
coroutine methods. Custom workflows registered only under ``imednet.workflows``
default to synchronous-only and raise :exc:`TypeError` on
:class:`~imednet.sdk.AsyncImednetSDK` unless they also register an
``imednet.async_workflows`` adapter or declare ``supports_async = True``.

A namespace-level plugin factory registered under ``imednet.plugins`` satisfies
:class:`~imednet.plugins.PluginProtocol` (or :class:`~imednet.plugins.SinksPluginProtocol`,
returning :class:`~imednet.plugins.SinksNamespaceProtocol` /
:class:`~imednet.plugins.AsyncSinksNamespaceProtocol` or
:class:`~imednet.plugins.SyncWorkflowsNamespaceProtocol` /
:class:`~imednet.plugins.AsyncWorkflowsNamespaceProtocol`):

.. testcode::

    from typing import Any, Protocol
    from imednet.plugins import SyncWorkflowsNamespaceProtocol

    class PluginProtocol(Protocol):
        def __call__(self, sdk_instance: Any) -> SyncWorkflowsNamespaceProtocol: ...

The built-in ``imednet-workflows`` package registers all nine workflows
symmetrically across ``imednet.workflows``
(:class:`~imednet.plugins.SyncWorkflowsNamespaceProtocol` on ``ImednetSDK``) and
``imednet.async_workflows``
(:class:`~imednet.plugins.AsyncWorkflowsNamespaceProtocol` on
``AsyncImednetSDK``), while ``imednet-sinks`` binds ``export_to_mongodb``,
``export_to_neo4j``, and ``export_to_snowflake`` on both ``ImednetSDK.sinks``
and ``AsyncImednetSDK.sinks``:

* ``data_extraction``
* ``duckdb_centralizer``
* ``query_management``
* ``record_mapper``
* ``record_update``
* ``register_subjects``
* ``subject_data``
* ``uat``
* ``uat_inspector``

Minimal working example
-----------------------

.. testcode::

    # myplugin/workflows.py
    from __future__ import annotations

    from typing import TYPE_CHECKING, Any

    if TYPE_CHECKING:
        from imednet.sdk import ImednetSDK


    class MyDataExtraction:
        def __init__(self, sdk: "ImednetSDK") -> None:
            self._sdk = sdk

        def run(self, study_key: str) -> list[Any]:
            return list(self._sdk.records.list(study_key=study_key))

Registering the entry point
---------------------------

Add the following standard PEP 621 entry-point table to your package's
``pyproject.toml`` to register a workflow on ``sdk.workflows`` (or a namespace
plugin under ``imednet.plugins``):

.. code-block:: toml

    [project.entry-points."imednet.workflows"]
    my_extraction = "myplugin.workflows:MyDataExtraction"

    [project.entry-points."imednet.plugins"]
    sinks = "imednet_sinks.plugin:create_sinks"

Or, when using ``setuptools``, in ``setup.cfg``:

.. code-block:: ini

    [options.entry_points]
    imednet.workflows =
        my_extraction = myplugin.workflows:MyDataExtraction
    imednet.plugins =
        sinks = imednet_sinks.plugin:create_sinks

Once installed (e.g. ``pip install myplugin``), the SDK discovers and
instantiates registered workflows automatically:

.. testcode::

    from imednet import ImednetSDK

    sdk = ImednetSDK(api_key="...", security_key="...")
    records = sdk.workflows.data_extraction.extract_records_by_criteria("MY_STUDY")

Error handling
--------------

:exc:`~imednet.errors.PluginLoadError` is raised when loading a namespace
plugin from ``imednet.plugins`` if:

* More than one plugin is registered for the same entry-point name in the
  ``imednet.plugins`` group.
* The entry point fails to import (e.g. broken dependency).
* The loaded object is not callable.
* The factory raises :exc:`TypeError` when called with the SDK instance.

:exc:`~imednet.errors.PluginLoadError` is a subclass of
:exc:`~imednet.errors.ImednetError` so it can be caught uniformly:

.. testcode::

    from imednet import ImednetSDK
    from imednet.errors import ImednetError

    try:
        sdk = ImednetSDK(api_key="...", security_key="...")
    except ImednetError as exc:
        print(f"SDK initialisation failed: {exc}")

API reference
-------------

.. autoclass:: imednet.plugins.PluginProtocol
   :members:
   :undoc-members:
   :noindex:

.. autoclass:: imednet.plugins.SyncWorkflowsNamespaceProtocol
   :members:
   :undoc-members:
   :noindex:

.. autoclass:: imednet.plugins.AsyncWorkflowsNamespaceProtocol
   :members:
   :undoc-members:
   :noindex:

.. autoclass:: imednet.plugins.SinksNamespaceProtocol
   :members:
   :undoc-members:
   :noindex:

.. autoclass:: imednet.plugins.AsyncSinksNamespaceProtocol
   :members:
   :undoc-members:
   :noindex:

.. autoexception:: imednet.errors.PluginLoadError
   :noindex:
