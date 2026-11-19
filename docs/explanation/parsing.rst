Model Parsing
=============

The ``imednet.core.parsing`` module provides a centralized strategy for parsing
API responses into Pydantic models.

Overview
--------

All endpoints in the iMednet SDK parse JSON responses from the API into typed
Pydantic model instances. The parsing module provides a consistent approach to
this conversion, eliminating duplicated parsing logic across the codebase.

Strategy Pattern
----------------

The parsing module implements a strategy pattern that checks for custom parsing
methods before falling back to the default Pydantic validation:

1. If a model has a custom ``from_json`` classmethod, use it
2. Otherwise, fall back to Pydantic's ``model_validate``

This allows models to implement custom deserialization logic when needed while
providing a sensible default for standard models.

API Reference
-------------

.. automodule:: imednet.core.parsing
   :members:
   :show-inheritance:
   :noindex:

Usage Examples
--------------

Function-based Parsing
^^^^^^^^^^^^^^^^^^^^^^

Use ``get_model_parser`` to get a parsing function for a specific model:

.. testcode::

   from imednet.core.parsing import get_model_parser
   from imednet.models.studies import Study

   # Get the parser function
   study_parser = get_model_parser(Study)

   # Parse API response data
   study_data = {"study_name": "Clinical Trial", "study_key": "CT-001"}
   study = study_parser(study_data)

Class-based Parsing
^^^^^^^^^^^^^^^^^^^

Use ``ModelParser`` for repeated parsing operations with the same model:

.. testcode::

   from imednet.core.parsing import ModelParser
   from imednet.models.subjects import Subject

   # Create a parser instance
   subject_parser = ModelParser(Subject)

   # Parse a single item
   subject_data = {"subject_key": "SUB-001"}
   subject = subject_parser.parse(subject_data)

   # Parse multiple items
   api_response = [{"subject_key": "SUB-001"}]
   subjects = subject_parser.parse_many(api_response)

Custom Parsing Methods
^^^^^^^^^^^^^^^^^^^^^^

Models can implement custom parsing logic by providing a ``from_json`` classmethod:

.. testcode::

   from pydantic import BaseModel
   from typing import Any

   class CustomModel(BaseModel):
       field1: str
       field2: int

       @classmethod
       def from_json(cls, data: Any) -> "CustomModel":
           # Custom preprocessing
           processed = {
               "field1": data.get("customField1", "").strip(),
               "field2": int(data.get("customField2", 0)),
           }
           return cls.model_validate(processed)

   # The parser will automatically use the custom from_json method
   from imednet.core.parsing import get_model_parser
   custom_parser = get_model_parser(CustomModel)

Integration with Endpoints
---------------------------

All endpoints use this centralized parsing strategy internally. The
``ListGetEndpointMixin`` uses ``get_model_parser`` to resolve the appropriate
parsing function for each endpoint's model type.

This ensures consistent behavior across the SDK while allowing endpoints to
customize parsing when needed by overriding the ``_parse_item`` method.

Best Practices
--------------

1. **Use the centralized parser**: Always use ``get_model_parser`` or ``ModelParser``
   rather than calling ``from_json`` or ``model_validate`` directly. This ensures
   the parsing strategy is applied consistently.

2. **Implement from_json for complex models**: If your model requires preprocessing
   of API data before validation, implement a ``from_json`` classmethod rather than
   complex field validators.

3. **Keep parsing logic simple**: The parser should focus on data transformation,
   not business logic. Complex operations belong in separate workflow classes.
