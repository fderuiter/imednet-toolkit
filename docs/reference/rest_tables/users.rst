.. _rest_resource_users:

Users
~~~~~

User accounts, role assignments, and investigator study access permissions.

List users
^^^^^^^^^^

.. rubric:: HTTP Route

``GET /api/v1/edc/studies/{studyKey}/users``

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
     - Maximum number of records to return per page.
   * - ``sort``
     - String
     - None
     - Sorting expression (e.g. ``studyKey,asc`` or ``dateCreated,desc``).
   * - ``includeInactive``
     - Boolean
     - ``false``
     - Whether to include inactive users in the returned result set.

.. rubric:: Status Codes

.. list-table::
   :widths: 15 25 60
   :header-rows: 1

   * - Status
     - Reason
     - Description
   * - ``200``
     - OK
     - Successful response: list users.
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
               "userId": "<string>",
               "login": "<string>",
               "firstName": "<string>",
               "lastName": "<string>",
               "email": "<string>",
               "userActiveInStudy": "<boolean>",
               "roles": []
           },
           {
               "userId": "<string>",
               "login": "<string>",
               "firstName": "<string>",
               "lastName": "<string>",
               "email": "<string>",
               "userActiveInStudy": "<boolean>"
           }
       ]
   }

