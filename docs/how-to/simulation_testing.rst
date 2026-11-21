Offline Testing with ``SimulationTransport``
============================================

:class:`~imednet.simulation.transport.SimulationTransport` and
:class:`~imednet.simulation.state.SimulationState` provide an in-memory HTTP
transport implementing the iMednet REST API contract without opening network
sockets or requiring live credentials.

Enabling Simulation Mode in ``ImednetSDK``
------------------------------------------

Pass ``simulation_mode=True`` to :class:`~imednet.ImednetSDK` or
:class:`~imednet.AsyncImednetSDK`—or set ``IMEDNET_SIMULATION_MODE=true`` in the
environment—to route all endpoint requests through ``SimulationTransport`` backed
by deterministic fixtures for ``Study`` ``"STUDY"`` (including ``Site`` ``"SITE"``,
``Form`` definitions ``"F_DM"`` and ``"F_VS"``, ``Subject`` ``"SUBJ-001"``,
``Visit`` / ``Interval`` schedules, ``Record`` items, ``RecordRevision`` audit
entries, and ``Query`` items):

.. testcode::

   from imednet import ImednetSDK
   from imednet.simulation import reset_default_simulation_state

   with ImednetSDK(simulation_mode=True) as sdk:
       studies = list(sdk.studies.list())
       forms = list(sdk.forms.list(study_key="STUDY"))
       job = sdk.records.create(
           "STUDY",
           [
               {
                   "formKey": "F_VS",
                   "siteName": "SITE",
                   "subjectKey": "SUBJ-001",
                   "intervalName": "Baseline",
                   "data": {"V_WEIGHT": 74.0},
               }
           ],
       )
       status = sdk.poll_job("STUDY", job.batch_id or "", interval=0.05)
       assert status.is_successful is True

   reset_default_simulation_state()

Running the CLI in Simulation Mode
----------------------------------

Pass ``--simulation`` on the root ``imednet`` command or any API-backed subcommand
(or set ``IMEDNET_SIMULATION_MODE=true``) to exercise CLI tables, JSON envelopes,
exports, write commands, workflows, and the Textual TUI offline without API keys:

.. code-block:: bash

   imednet studies list --simulation --no-interactive
   imednet forms list STUDY --simulation --format json
   imednet datadict from-api STUDY --simulation -o sim_datadict.json
   imednet export json STUDY ./sim_records.json --simulation --no-interactive
   imednet subject-data STUDY SUB1 --simulation --format json
   imednet tui STUDY --simulation

Isolated Per-Test ``SimulationState`` Injection
-----------------------------------------------

By default, ``ImednetSDK(simulation_mode=True)`` shares a process-local state
accessible via :func:`~imednet.simulation.get_default_simulation_state` and
resettable with :func:`~imednet.simulation.reset_default_simulation_state`. When
running parallel unit tests that mutate ``Subject`` or ``Record`` collections,
instantiate an isolated ``SimulationState`` + ``SimulationTransport`` and attach
custom ``httpx.Client`` / ``httpx.AsyncClient`` transports directly:

.. testcode::

   from imednet.sdk import ImednetSDK
   from imednet.simulation.state import SimulationState
   from imednet.simulation.transport import SimulationTransport

   isolated_state = SimulationState()
   transport = SimulationTransport(state=isolated_state)

   with ImednetSDK(simulation_mode=True, transport=transport) as isolated_sdk:
       initial_records = list(isolated_sdk.records.list(study_key="STUDY"))
       assert len(initial_records) >= 1
