"""Official Synology File Station API client, using only Python's standard library."""

from .client import (
    APIError,
    APIInfo,
    AuthenticationError,
    DSMClient,
    DSMError,
    SearchCancelled,
    SearchResult,
    SearchTimeout,
)

__all__ = [
    "APIError", "APIInfo", "AuthenticationError", "DSMClient", "DSMError",
    "SearchResult", "SearchTimeout", "SearchCancelled",
]
