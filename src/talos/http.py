"""Small HTTP boundary with bounded requests and sanitized failures."""

import httpx

from .errors import AuthenticationError, SynchronizationError


def request(client: httpx.Client, method: str, path: str, **kwargs):
    try:
        response = client.request(method, path, **kwargs)
    except Exception:
        message = "API request failed"
        if method in {"POST", "PATCH"}:
            message += "; write outcome unknown, observe current state before retrying"
        raise SynchronizationError(message) from None
    if response.status_code in {401, 403} or 300 <= response.status_code < 400:
        raise AuthenticationError("API authentication was rejected or redirected")
    expected = 201 if method == "POST" else 200
    if response.status_code != expected:
        raise SynchronizationError(f"API {method} failed (HTTP {response.status_code})")
    try:
        return response.json()
    except Exception:
        message = "API returned invalid JSON"
        if method in {"POST", "PATCH"}:
            message += "; write outcome unknown, observe current state before retrying"
        raise SynchronizationError(message) from None


def client(url, headers):
    return httpx.Client(
        base_url=url,
        headers=headers,
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=False,
        trust_env=False,
    )
