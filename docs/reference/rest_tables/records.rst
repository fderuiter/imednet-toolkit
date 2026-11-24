.. _rest_resource_records:

Records
~~~~~~~

Clinical data records, form instances, and transactional record creation endpoints.

List records
^^^^^^^^^^^^

.. rubric:: HTTP Route

``GET /api/v1/edc/studies/{studyKey}/records``

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
     - Maximum number of records to return per page (maximum ``500``).
   * - ``sort``
     - String
     - ``recordId,asc``
     - Sorting expression (e.g. ``recordId,asc`` or ``dateCreated,desc``).
   * - ``filter``
     - String
     - None
     - Attribute query filter expression (see :ref:`filter_syntax`).
   * - ``recordDataFilter``
     - String
     - None
     - Dynamic question data filter expression (see :ref:`filter_syntax`).

.. rubric:: Status Codes

.. list-table::
   :widths: 15 25 60
   :header-rows: 1

   * - Status
     - Reason
     - Description
   * - ``200``
     - OK
     - Successful response: list records.
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
               "studyKey": "<string>",
               "intervalId": "<integer>",
               "formId": "<integer>",
               "formKey": "<string>",
               "siteId": "<integer>",
               "recordId": "<integer>",
               "recordOid": "<string>",
               "recordType": "<string>",
               "recordStatus": "<string>",
               "deleted": "<boolean>",
               "dateCreated": "<string>",
               "dateModified": "<string>",
               "subjectId": "<integer>",
               "subjectOid": "<string>",
               "subjectKey": "<string>",
               "visitId": "<integer>",
               "parentRecordId": "<integer>",
               "recordData": {},
               "lastUpdated": "<string>",
               "keywords": [
                   {
                       "keywordName": "<string>",
                       "keywordKey": "<string>",
                       "keywordId": 1,
                       "dateAdded": "2020-07-20 14:05:00"
                   }
               ]
           },
           {
               "studyKey": "<string>",
               "intervalId": "<integer>",
               "formId": "<integer>",
               "formKey": "<string>",
               "siteId": "<integer>",
               "recordId": "<integer>",
               "recordOid": "<string>",
               "recordType": "<string>",
               "recordStatus": "<string>",
               "deleted": "<boolean>",
               "dateCreated": "<string>",
               "dateModified": "<string>",
               "subjectId": "<integer>",
               "subjectOid": "<string>",
               "subjectKey": "<string>",
               "visitId": "<integer>",
               "parentRecordId": "<integer>",
               "recordData": {}
           }
       ]
   }

Create records
^^^^^^^^^^^^^^

.. rubric:: HTTP Route

``POST /api/v1/edc/studies/{studyKey}/records``

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
   * - ``Content-Type``
     - String
     - Required
     - Request media type. Must be ``application/json``.
   * - ``x-email-notify``
     - Boolean / String
     - Optional
     - Enable email notification on record submission (``true``, ``false``, or recipient email address).

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

.. rubric:: Request Body Schema

The request body must be a JSON array of record objects (``[{...}, ...]``). Each record object in the array supports the following fields:

.. list-table::
   :widths: 25 15 15 45
   :header-rows: 1

   * - Field
     - Type
     - Required
     - Description
   * - ``formKey``
     - String
     - Conditional
     - Case Report Form (CRF) unique key identifier. Either ``formKey`` or ``formId`` is required.
   * - ``formId``
     - Integer
     - Conditional
     - Numeric CRF identifier accepted in place of ``formKey`` (either ``formKey`` or ``formId`` is required).
   * - ``siteName``
     - String
     - Conditional
     - Clinical research site name. Either ``siteName`` or ``siteId`` is required when registering a new subject.
   * - ``siteId``
     - Integer
     - Conditional
     - Numeric site identifier accepted in place of ``siteName`` when registering a new subject.
   * - ``subjectKey``
     - String
     - Conditional
     - Participant subject key. One of ``subjectKey``, ``subjectId``, or ``subjectOid`` is required when creating or updating records for an existing subject (omit when registering a new subject).
   * - ``subjectId``
     - Integer
     - Conditional
     - Numeric participant subject identifier accepted in place of ``subjectKey``.
   * - ``subjectOid``
     - String
     - Conditional
     - System-assigned participant subject OID accepted in place of ``subjectKey``.
   * - ``intervalName``
     - String
     - Conditional
     - Scheduled study visit or interval name. Either ``intervalName`` or ``intervalId`` is required when updating a scheduled visit record.
   * - ``intervalId``
     - Integer
     - Conditional
     - Numeric study visit or interval identifier accepted in place of ``intervalName`` for scheduled visit records.
   * - ``recordId``
     - Integer
     - Conditional
     - Existing numeric record identifier used when updating a specific unscheduled record.
   * - ``data``
     - Object
     - Required
     - Clinical data dictionary mapping item/question variable names to recorded values.

.. rubric:: Sample Request Payload

.. code-block:: json

   [
     {
       "formKey": "<string>",
       "subjectKey": "<string>",
       "siteName": "<string>",
       "intervalName": "<string>",
       "data": {}
     },
     {
       "formKey": "<string>",
       "subjectKey": "<string>",
       "siteName": "<string>",
       "intervalName": "<string>",
       "data": {}
     }
   ]

.. rubric:: Status Codes

.. list-table::
   :widths: 15 25 60
   :header-rows: 1

   * - Status
     - Reason
     - Description
   * - ``202``
     - Accepted
     - Successful response: create records.
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
       "jobId": "<string>",
       "batchId": "<string>",
       "state": "OPEN",
       "dateCreated": "",
       "dateFinished": "",
       "dateStarted": ""
   }
