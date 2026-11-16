"""Operation for executing get requests via direct path.

This module encapsulates the logic for fetching and parsing a single resource
from the API using its ID.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from imednet.core.endpoint.dispatch import Operation
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol

T = TypeVar("T")


class PathGetOperation(Operation[T]):
    """Operation for executing get requests via direct path.

    Encapsulates the logic for making the HTTP request, handling empty
    responses (not found), and parsing the result.
    """

    def __init__(
        self,
        path: str,
        parse_func: Callable[[Any], T],
        not_found_func: Callable[[], None],
    ) -> None:
        """Initialize the path get operation.

        Args:
            path: The API endpoint path.
            parse_func: A function to parse a raw JSON item into the model T.
            not_found_func: A callback to raise the appropriate not found error.
        """
        self.path = path
        self.parse_func = parse_func
        self.not_found_func = not_found_func

    def _process_response(self, response: Any) -> T:
        """Process the raw HTTP response."""
        data = response.json()
        if not data:
            self.not_found_func()
        return self.parse_func(data)

    def execute_sync(self, client: RequesterProtocol, parse_func: Any = None) -> T:
        """Execute synchronous get request.

        Args:
            client: The synchronous HTTP client.
            parse_func: Optional parse function (unused; instance parse_func is used).

        Returns:
            The parsed item.
        """
        response = client.get(self.path)
        return self._process_response(response)

    async def execute_async(self, client: AsyncRequesterProtocol, parse_func: Any = None) -> T:
        """Execute asynchronous get request.

        Args:
            client: The asynchronous HTTP client.
            parse_func: Optional parse function (unused; instance parse_func is used).

        Returns:
            The parsed item.
        """
        response = await client.get(self.path)
        return self._process_response(response)
