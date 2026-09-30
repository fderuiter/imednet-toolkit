Plugin Authoring Guide
======================

The iMednet SDK monorepo contains the following first-party packages:

* :doc:`imednet </reference/api/core>` (Core SDK) — The foundational API client and SDK.
* :doc:`imednet-workflows </reference/api/plugins-workflows>` — Opinionated data-extraction and query-management workflows.
* :doc:`imednet-sinks </reference/api/plugins-sinks>` — Export sinks for data destinations.
* :doc:`apache-airflow-providers-imednet </reference/api/providers-airflow>` — Apache Airflow integrations.

The built-in ``imednet-workflows`` package is the reference implementation for
third-party plugins.  Any third-party package can register its own plugin using
the standard `Python entry-point`_ mechanism.

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

During :class:`~imednet.sdk.ImednetSDK` (and
:class:`~imednet.sdk.AsyncImednetSDK`) initialisation the SDK queries the
``imednet.plugins`` entry-point group for an entry named ``"workflows"``.  If
exactly one is found the SDK loads the factory callable and calls it with
itself as the argument.  The returned object is stored as ``sdk.workflows``.

If the plugin is **not** installed, ``sdk.workflows`` is a placeholder that
raises :exc:`ImportError` with a helpful message on first attribute access.

If the plugin **is** installed but fails to load (broken import, wrong
interface, …) a :exc:`~imednet.errors.PluginLoadError` is raised at SDK
construction time.

Plugin contract
---------------

A plugin package must expose a single factory callable.  The factory must
satisfy :class:`~imednet.plugins.PluginProtocol`:

.. testcode::

    from typing import Any, Protocol
    from imednet.plugins import WorkflowsNamespaceProtocol

    class PluginProtocol(Protocol):
        def __call__(self, sdk_instance: Any) -> WorkflowsNamespaceProtocol: ...

The object returned by the factory must satisfy
:class:`~imednet.plugins.WorkflowsNamespaceProtocol` — it must expose at
least the following attributes (any type):

* ``data_extraction``
* ``query_management``
* ``record_mapper``
* ``record_update``
* ``subject_data``

Minimal working example
-----------------------

.. testcode::

    # myplugin/namespace.py
    from __future__ import annotations

    from typing import TYPE_CHECKING, Any

    if TYPE_CHECKING:
        from imednet.sdk import ImednetSDK


    class MyDataExtraction:
        def __init__(self, sdk: "ImednetSDK") -> None:
            self._sdk = sdk

        def run(self, study_key: str) -> list[Any]:
            return list(self._sdk.records.list(study_key=study_key))


    class MyWorkflows:
        def __init__(self, sdk: "ImednetSDK") -> None:
            self.data_extraction = MyDataExtraction(sdk)
            self.query_management = None   # implement as needed
            self.record_mapper = None
            self.record_update = None
            self.subject_data = None


    def create_workflows(sdk: "ImednetSDK") -> MyWorkflows:
        """Entry-point factory — called by ImednetSDK at initialisation."""
        return MyWorkflows(sdk)

Registering the entry point
---------------------------

Add the following to your package's ``pyproject.toml``:

.. code-block:: toml

    [tool.hatch.plugins."imednet.plugins"]
    workflows = "myplugin.namespace:create_workflows"

Or, when using ``setuptools``, in ``setup.cfg``:

.. code-block:: ini

    [options.entry_points]
    imednet.plugins =
        workflows = myplugin.namespace:create_workflows

Once installed (e.g. ``pip install myplugin``), the SDK will discover and
load the plugin automatically:

.. testcode::

    from imednet import ImednetSDK

    sdk = ImednetSDK(api_key="...", security_key="...")
    records = sdk.workflows.data_extraction.extract_records_by_criteria("MY_STUDY")

Error handling
--------------

:exc:`~imednet.errors.PluginLoadError` is raised when:

* More than one ``"workflows"`` plugin is registered in the
  ``imednet.plugins`` entry-point group.
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

.. autoclass:: imednet.plugins.WorkflowsNamespaceProtocol
   :members:
   :undoc-members:

.. autoexception:: imednet.errors.PluginLoadError
