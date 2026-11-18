.. _rest_resource_jobs:

Jobs
~~~~

Asynchronous batch processing status and progress monitoring for background operations.

Retrieve job status
^^^^^^^^^^^^^^^^^^^

.. rubric:: HTTP Route

``GET /api/v1/edc/studies/{studyKey}/jobs/{batchId}``

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
   * - ``batchId``
     - String
     - Required
     - Unique batch tracking identifier for asynchronous record creation job.

.. rubric:: Status Codes

.. list-table::
   :widths: 15 25 60
   :header-rows: 1

   * - Status
     - Reason
     - Description
   * - ``200``
     - OK
     - Successful response: retrieve job status.
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
     "state": "COMPLETED",
     "dateCreated": "<string>",
     "dateStarted": "<string>",
     "dateFinished": "<string>",
     "progress": 100,
     "resultUrl": "<string>"
   }
