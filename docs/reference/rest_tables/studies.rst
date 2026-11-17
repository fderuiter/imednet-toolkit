.. _rest_resource_studies:

Studies
~~~~~~~

Clinical study protocols, site configuration metadata, and trial registry records.

List studies
^^^^^^^^^^^^

.. rubric:: HTTP Route

``GET /api/v1/edc/studies``

.. rubric:: Request Headers

.. list-table::
   :widths: 25 15 15 45
   :header-rows: 1

   * - Header
     - Type
     - Required
     - Description
   * - ``x-api-key``
     - String
     - Required
     - API key credential for client authentication.
   * - ``x-imn-security-key``
     - String
     - Required
     - User security key token for request authorization.
   * - ``Accept``
     - String
     - Required
     - Response media type. Must be ``application/json``.

.. rubric:: Query Parameters

.. list-table::
   :widths: 25 15 15 45
   :header-rows: 1

   * - Parameter
     - Type
     - Default
     - Description
   * - ``page``
     - Integer
     - ``0``
     - Zero-based page index for paged results.
   * - ``size``
     - Integer
     - ``25``
     - Maximum number of records to return per page.
   * - ``sort``
     - String
     - ``studyKey,asc``
     - Sorting expression (e.g. ``studyKey,asc`` or ``dateCreated,desc``).
   * - ``filter``
     - String
     - None
     - Attribute query filter expression (see :ref:`filter_syntax`).

.. rubric:: Status Codes

.. list-table::
   :widths: 15 25 60
   :header-rows: 1

   * - Status
     - Reason
     - Description
   * - ``200``
     - OK
     - Successful response: list studies.
   * - ``400``
     - Bad Request
     - Invalid query parameters, missing required fields, or malformed JSON payload.
   * - ``401``
     - Unauthorized
     - Missing or invalid ``x-api-key`` or ``x-imn-security-key`` headers.
   * - ``403``
     - Forbidden
     - Insufficient permissions to access clinical study resources or invalid studyKey.
   * - ``404``
     - Not Found
     - Resource, study key, or record identifier not found.
   * - ``429``
     - Too Many Requests
     - API rate limit threshold exceeded for current credentials.
   * - ``500``
     - Server Error
     - Internal server error occurred during request processing.

.. rubric:: Sample Response Payload

.. code-block:: json

   {
     "metadata": {
       "status": "<string>",
       "method": "<string>",
       "path": "<string>",
       "timestamp": "<string>",
       "error": {}
     },
     "pagination": {
       "currentPage": "<integer>",
       "size": "<integer>",
       "totalPages": "<integer>",
       "totalElements": "<integer>",
       "sort": [
         {
           "property": "<string>",
           "direction": "<string>"
         },
         {
           "property": "<string>",
           "direction": "<string>"
         }
       ]
     },
     "data": [
       {
         "sponsorKey": "<string>",
         "studyKey": "<string>",
         "studyId": "<integer>",
         "studyName": "<string>",
         "studyDescription": "<string>",
         "studyType": "<string>",
         "dateCreated": "<string>",
         "dateModified": "<string>"
       },
       {
         "sponsorKey": "<string>",
         "studyKey": "<string>",
         "studyId": "<integer>",
         "studyName": "<string>",
         "studyDescription": "<string>",
         "studyType": "<string>",
         "dateCreated": "<string>",
         "dateModified": "<string>"
       }
     ]
   }

Retrieve a study
^^^^^^^^^^^^^^^^

.. rubric:: HTTP Route

``GET /api/v1/edc/studies/{studyKey}``

.. rubric:: Request Headers

.. list-table::
   :widths: 25 15 15 45
   :header-rows: 1

   * - Header
     - Type
     - Required
     - Description
   * - ``x-api-key``
     - String
     - Required
     - API key credential for client authentication.
   * - ``x-imn-security-key``
     - String
     - Required
     - User security key token for request authorization.
   * - ``Accept``
     - String
     - Required
     - Response media type. Must be ``application/json``.

.. rubric:: Path Variables

.. list-table::
   :widths: 25 15 15 45
   :header-rows: 1

   * - Variable
     - Type
     - Required
     - Description
   * - ``studyKey``
     - String
     - Required
     - Unique key identifying the clinical study protocol.

.. rubric:: Status Codes

.. list-table::
   :widths: 15 25 60
   :header-rows: 1

   * - Status
     - Reason
     - Description
   * - ``200``
     - OK
     - Successful response: retrieve a study.
   * - ``400``
     - Bad Request
     - Invalid query parameters, missing required fields, or malformed JSON payload.
   * - ``401``
     - Unauthorized
     - Missing or invalid ``x-api-key`` or ``x-imn-security-key`` headers.
   * - ``403``
     - Forbidden
     - Insufficient permissions to access clinical study resources or invalid studyKey.
   * - ``404``
     - Not Found
     - Resource, study key, or record identifier not found.
   * - ``429``
     - Too Many Requests
     - API rate limit threshold exceeded for current credentials.
   * - ``500``
     - Server Error
     - Internal server error occurred during request processing.

.. rubric:: Sample Response Payload

.. code-block:: json

   {
       "sponsorKey": "<string>",
       "studyKey": "<string>",
       "studyId": "<integer>",
       "studyName": "<string>",
       "studyDescription": "<string>",
       "studyType": "<string>",
       "dateCreated": "<string>",
       "dateModified": "<string>",
       "lastUpdated": "<string>"
   }
