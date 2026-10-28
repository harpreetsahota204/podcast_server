"""Single entry point for outbound HTTP requests.

`urllib.request.urlopen` also accepts `file://` and other schemes, which would let a
crafted URL read local files; every request goes through `open_https` instead.
"""

import urllib.request
from http.client import HTTPResponse
from urllib.parse import urlparse

USER_AGENT = "podcast_server/1.0"
# Generous enough for slow CDNs, short enough that a dead host can't hang an MCP call forever.
DEFAULT_TIMEOUT_SECONDS = 60


def open_https(url: str, extra_headers: dict[str, str] | None = None, data: bytes | None = None) -> HTTPResponse:
    """Open an HTTPS URL with the engine's User-Agent and a timeout; reject any other scheme.

    Passing `data` sends a POST with that body (e.g. a GraphQL query); otherwise it's a GET.
    """
    if urlparse(url).scheme != "https":
        raise ValueError(f"Only https:// URLs are allowed, got: {url}")
    headers = {"User-Agent": USER_AGENT, **(extra_headers or {})}
    request = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 - scheme checked above
    return urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)  # noqa: S310
