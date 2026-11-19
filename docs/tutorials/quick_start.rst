Quick Start
===========

This page walks through a minimal example of using the SDK.

.. pyodide-button:: Activate Interactive Runtime

Installation
------------

.. code-block:: bash

   # PyPI release
   pip install imednet

.. include:: /_includes/install_extensions.rst

.. code-block:: bash

   # Workflow plugin package
   pip install imednet-workflows

   # Airflow provider package (core hook/operator/sensor support)
   pip install "apache-airflow>=3.2.0,<4.0.0" apache-airflow-providers-imednet


Basic Usage
-----------

Set your credentials by copying the environment template or exporting them directly:

.. code-block:: bash

   cp .env.example .env
   export IMEDNET_API_KEY="your_api_key"
   export IMEDNET_SECURITY_KEY="your_security_key"


Synchronous Example
-------------------

.. literalinclude:: /../examples/quick_start.py
   :language: python
   :start-after: # [start_quick_start]
   :end-before: # [end_quick_start]


Asynchronous Example
--------------------

.. literalinclude:: /../examples/async_quick_start.py
   :language: python
   :start-after: # [start_async_quick_start]
   :end-before: # [end_async_quick_start]


Custom retry logic can be provided via a ``RetryPolicy``:

.. testcode::

   from imednet import ImednetSDK
   from imednet.core.retry import RetryPolicy, RetryState
   from imednet.errors import ServerError

   class ServerRetry(RetryPolicy):
       def should_retry(self, state: RetryState) -> bool:
           return isinstance(state.exception, ServerError)

   sdk = ImednetSDK(api_key="mock", security_key="mock", retry_policy=ServerRetry())

See :doc:`/how-to/retry_policy` for more guidance on error handling and exponential
backoff.
