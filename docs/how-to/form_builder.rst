Form Builder
============

The Form Builder module allows maintainers to programmatically define visual ``Form`` layouts and submit them to the legacy iMedNet Form Designer. This bypasses manual drag-and-drop operations when automating legacy layout updates.

.. warning::
    **Legacy Browser Session Boundary:** This module targets the legacy web Form Designer endpoint (``formdez_save.php``), **not** the public iMednet REST API. It requires active browser session cookies (``PHPSESSID``) and anti-CSRF tokens (``CSRFKey``) scraped from an authenticated browser session—standard REST credentials (``IMEDNET_API_KEY`` and ``IMEDNET_SECURITY_KEY``) are not accepted by ``formdez_save.php``.

.. seealso::
    For canonical ``Data Dictionary`` workflows—including inspecting ``Form`` and ``Variable`` definitions, converting CDISC ODM 1.3 XML or REDCap CSV files, structural version diffing, and evaluating ``BusinessLogicRule`` edit checks—use :doc:`data_dictionary` instead of ``FormBuilder``.

Core Concepts
-------------

The module mimics the "State Dump" architecture of the Form Designer and uses a **Composite Design Pattern** to represent the visual ``Form`` layout.

Models and Architecture
~~~~~~~~~~~~~~~~~~~~~~~

The core building block is the :class:`~imednet.form_designer.models.Entity`. An Entity can be a visual container (such as a Table or Row) or a data capture control mapped to a ``Variable`` (such as a TextField or DateTimeField).

*   **TableProps**: Represents a visual section or table on a ``Form``.
*   **RowProps**: A horizontal arrangement of layout columns.
*   **ColumnProps**: A vertical container for layout controls.
*   **EntityProps**: A discriminated union of control properties.

This abstract structure is serialized into the hierarchical JSON payload expected by ``formdez_save.php``.

Process
~~~~~~~

1.  **Layout Definition**: Define the ``Form`` layout programmatically using :class:`~imednet.form_designer.FormBuilder`.
2.  **Serialization**: Call ``builder.build()`` to serialize the layout hierarchy into the ``formdez_save.php`` JSON payload.
3.  **Submission**: POST the payload through :class:`~imednet.form_designer.FormDesignerClient` using an active ``PHPSESSID`` cookie and ``CSRFKey`` token.

.. note::
    ``formdez_save.php`` overwrites the entire current layout state on save. Always verify the target ``--form-id``, ``--comm-id``, and next ``--revision`` number before submitting.

Usage
-----

The primary headless entry point for ``FormBuilder`` is ``examples/build_form_payload.py``.

Hybrid Script (Headless)
~~~~~~~~~~~~~~~~~~~~~~~~

All 7 session and target flags are required when running in headless mode (``--base-url`` may also fall back to ``IMEDNET_BASE_URL``):

.. code-block:: console

    uv run --with "imednet[cli]" python examples/build_form_payload.py \
        --preset "Demo Form" \
        --base-url "https://your.imednet.com" \
        --phpsessid "YOUR_SESSION_ID" \
        --csrf "YOUR_CSRF_KEY" \
        --form-id 12345 \
        --comm-id 999 \
        --revision 5

Presets
-------

Reusable ``Form`` layouts are defined as presets in ``imednet.form_designer.presets``. A preset is a function that accepts a :class:`~imednet.form_designer.FormBuilder` instance and populates its pages:

.. testcode::

    from imednet.form_designer import FormBuilder


    def build_my_form(builder: FormBuilder) -> None:
        builder.add_section_header("Subject Identification")
        builder.add_field(
            type="text",
            label="Subject Initials",
            question_name="INIT",
            required=True,
        )


    builder = FormBuilder()
    build_my_form(builder)
    layout = builder.build()
    assert len(layout.pages) == 1
    print(len(layout.pages))

See ``imednet.form_designer.presets`` for additional layout examples.

API Reference
-------------

.. toctree::
   :maxdepth: 2

   /reference/api/imednet.form_designer
