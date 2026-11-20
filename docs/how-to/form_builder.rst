Form Builder
============

The Form Builder module allows you to programmatically define form layouts and submit them to the legacy iMedNet Form Designer. This bypasses manual drag-and-drop operations, enabling automated form creation and updates.

.. warning::
    **Legacy Scope Only:** ``imednet.form_designer`` targets only visual page layout submission to the legacy internal Form Designer endpoint (``formdez_save.php``) and requires scraped browser session cookies (``PHPSESSID`` and ``CSRFKey``). It is **not** part of the official iMednet REST API and does **not** load Study Data Dictionaries, convert CDISC ODM XML, or validate Record edit check rules.

    For canonical Study metadata conversion, CDISC ODM / REDCap / CSV loading, ``BusinessLogicRule`` evaluation, and UAT verification, see :doc:`/how-to/data_dictionary` and :doc:`/how-to/workflows/uat_workflow`.

Core Concepts
-------------

The module mimics the "State Dump" architecture of the Form Designer and uses a **Composite Design Pattern** to represent the form structure.

Models and Architecture
~~~~~~~~~~~~~~~~~~~~~~~

The core building block is the :class:`~imednet.form_designer.models.Entity`. An Entity can be a visual container (like a Table or Row) or a data entry element (like a TextField or DateField).

*   **TableProps**: Represents a section or table on a form.
*   **RowProps**: A horizontal arrangement of columns.
*   **ColumnProps**: A vertical container for fields.
*   **EntityProps**: A discriminated union of specific field properties.

This abstract structure is then serialized into the complex JSON hierarchy expected by the legacy server.

Process
~~~~~~~

1.  **Layout Definition**: You define the form structure using the ``FormBuilder`` class.
2.  **Serialization**: The code converts this definition into the required JSON payload.
3.  **Submission**: The payload is POSTed to the legacy endpoint using your browser cookies.

.. note::
    This approach is "destructive" in the sense that it overwrites the current page state with the new layout. Always ensure you are working on the correct Revision number.

Usage
-----

The primary way to use the Form Builder is via the **Hybrid Script**.

Hybrid Script (Headless)
~~~~~~~~~~~~~~~~~~~~~~~~

For CI/CD or automated workflows, use the example script:

.. code-block:: console

    uv run --with "imednet[cli]" python examples/build_form_payload.py --preset "Demo Form" \
        --base-url "https://your.imednet.com" \
        --phpsessid "YOUR_SESSION_ID" \
        --csrf "YOUR_CSRF_KEY" \
        --form-id 12345 \
        --comm-id 999 \
        --revision 5

Presets
-------

Forms are defined as "Presets" in ``imednet/form_designer/presets.py``. A preset is simply a function that accepts a ``FormBuilder`` instance.

.. code-block:: python

    from imednet.form_designer import FormBuilder

    def build_my_form(builder: FormBuilder) -> None:
        builder.add_section_header("My New Section")

        builder.add_field(
            type="text",
            label="Subject Initials",
            question_name="INIT",
            required=True
        )

See ``imednet.form_designer.presets`` for more examples.

API Reference
-------------

.. toctree::
   :maxdepth: 2

   /reference/api/imednet.form_designer
