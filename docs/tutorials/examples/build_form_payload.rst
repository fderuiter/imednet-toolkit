Form Designer Payload Builder
=============================

Prerequisites
-------------

* ``imednet[cli]`` package installed
* Authenticated browser session tokens for the legacy Form Designer endpoint

Environment variables
---------------------

.. code-block:: bash

   export IMEDNET_BASE_URL="https://your.imednet.com"

Description
-----------

Build and upload Form layout definitions to the legacy Form Designer endpoint
(``formdez_save.php``) using ``FormBuilder``, ``FormDesignerClient``, and
session cookies rather than REST API keys.

.. literalinclude:: /../examples/build_form_payload.py
   :language: python
