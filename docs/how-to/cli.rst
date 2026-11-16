Command Line Interface (CLI)
============================

The package installs an ``imednet`` command that wraps common SDK features. The CLI
reads authentication details from environment variables. See :doc:`/reference/configuration`
for the full list and details on using an ``.env`` file.

.. argparse::
   :module: imednet.cli
   :func: get_parser
   :prog: imednet

Machine-readable core reads
---------------------------

The CLI uses standard-library argparse; install ``imednet[cli]`` only for optional
dotenv/logging helpers. Use the existing nested ``--help`` for command-specific flags.

.. code-block:: bash

   imednet studies list --format json
   imednet subjects list STUDY --filter subject_status=Screened --format json
   imednet records list STUDY --format json
   imednet jobs status STUDY BATCH --format json
   imednet jobs wait STUDY BATCH --timeout 60 --format json

Supported core list resources are studies, queries, variables, record-revisions,
sites, records, subjects and intervals. Core JSON reads emit exactly one envelope
with ``schemaVersion: "1.0"``, ``resource``, ``status: "complete"``, ``count`` and
``items``. Empty results use an empty items array. Item fields use the SDK models'
JSON aliases; the JSON view includes model fields beyond the human summary table.
Progress and diagnostics use stderr. A retrieval or serialization failure emits
no success response and exits 1; partial iterators are not reported as complete.
Usage failures exit 2, SDK-command interruption exits 130, and success/help exits 0.

``records list --output json`` still writes ``records.json``; it is distinct from
``--format json`` stdout and combining them is an error. Existing ``export``
commands, datadict JSON/schema/diff outputs and optional workflow command formats
retain their own contracts. This is not a global JSON wrapper for plugins.
See :doc:`/adr/0007-cli-framework-and-output-contracts` for the framework decision,
error/partial-output policy and compatibility inventory.
