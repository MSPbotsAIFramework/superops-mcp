# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Async client for the SuperOps.ai MSP GraphQL API."""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ._json import error_envelope
from .config import API_ENDPOINTS, DEFAULT_REGION

logger = logging.getLogger("superops_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# One connection pool for the process. It never holds credentials: the
# Authorization header is built per request from the caller's own token, so
# sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class SuperOpsError(Exception):
    def __init__(self, status_code: int, message: str, retry_after: float | None = None):
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"SuperOps API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


# GraphQL errors arrive with HTTP 200; map them onto HTTP-like statuses.
_ERROR_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"unauthori[sz]ed|unauthenticated|invalid (api )?token|authentication", re.I), 401),
    (re.compile(r"forbidden|permission|not allowed|access denied", re.I), 403),
    (re.compile(r"rate.?limit|too many requests|throttl", re.I), 429),
    (re.compile(r"not found|does not exist|no .* found", re.I), 404),
]


def graphql_error_status(errors: list[dict]) -> tuple[int, float | None]:
    retry_after = None
    for error in errors:
        extensions = error.get("extensions") if isinstance(error.get("extensions"), dict) else {}
        if isinstance(extensions.get("retryAfter"), (int, float)):
            retry_after = float(extensions["retryAfter"])
    if retry_after is not None:
        return 429, retry_after
    text = " ".join(
        f"{e.get('message', '')} {(e.get('extensions') or {}).get('code', '') if isinstance(e.get('extensions'), dict) else ''}"
        for e in errors
    )
    for pattern, status in _ERROR_PATTERNS:
        if pattern.search(text):
            return status, None
    return 400, None


@dataclass(frozen=True)
class SuperOpsCredentials:
    api_token: str
    subdomain: str
    region: str = DEFAULT_REGION

    def __repr__(self) -> str:  # never leak the token into logs or tracebacks
        return f"SuperOpsCredentials(subdomain={self.subdomain!r}, region={self.region!r}, api_token=<redacted>)"


class SuperOpsClient:
    """GraphQL client for one caller's API token and SuperOps subdomain."""

    def __init__(self, credentials: SuperOpsCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.endpoint = API_ENDPOINTS.get(credentials.region, API_ENDPOINTS[DEFAULT_REGION])

    @property
    def subdomain(self) -> str:
        return self._credentials.subdomain

    @property
    def region(self) -> str:
        return self._credentials.region

    def _client(self) -> httpx.AsyncClient:
        return self._http or _get_shared_http_client()

    async def query(self, document: str, variables: dict[str, Any] | None = None, *, operation: str = "query") -> dict:
        """Run a GraphQL document and return its ``data``.

        Mutations are retried only on rate limiting (refused before processing),
        so a write never runs twice; queries are also retried on 5xx and
        network errors.
        """
        headers = {
            "Authorization": f"Bearer {self._credentials.api_token}",
            "CustomerSubDomain": self._credentials.subdomain,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {"query": document, "variables": variables or {}}
        is_read = operation == "query"
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            error: SuperOpsError | None = None
            data: dict | None = None
            resp: httpx.Response | None = None
            try:
                resp = await self._client().post(self.endpoint, json=payload, headers=headers)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, SuperOpsError(0, "Timed out calling SuperOps")
            except httpx.RequestError as e:
                status, error = 0, SuperOpsError(0, f"Network error calling SuperOps: {type(e).__name__}")

            if resp is not None:
                if status >= 400:
                    error = SuperOpsError(status, _http_error_message(resp))
                else:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    if not isinstance(body, dict):
                        error = SuperOpsError(502, "SuperOps returned a response that is not GraphQL JSON")
                    elif body.get("errors"):
                        errors = [e for e in body["errors"] if isinstance(e, dict)]
                        mapped, retry_after = graphql_error_status(errors)
                        status = mapped
                        error = SuperOpsError(mapped, _graphql_message(errors), retry_after)
                    elif not isinstance(body.get("data"), dict):
                        error = SuperOpsError(502, "SuperOps returned no data")
                    else:
                        data = body["data"]

            logger.info(
                "superops_api operation=%s status=%s ms=%d attempt=%d",
                operation, status, (time.monotonic() - t0) * 1000, attempt,
            )
            if data is not None:
                return data

            retryable = status == 429 or (is_read and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, error, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            assert error is not None
            raise error


def _retry_delay(resp: httpx.Response | None, error: SuperOpsError | None, attempt: int) -> float:
    if error is not None and error.retry_after is not None:
        return max(0.0, min(error.retry_after, _MAX_BACKOFF_SECONDS))
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _graphql_message(errors: list[dict]) -> str:
    messages = [str(e.get("message") or "GraphQL error") for e in errors] or ["GraphQL error"]
    if len(messages) == 1:
        return messages[0][:500]
    return f"{len(messages)} GraphQL errors: " + " | ".join(messages)[:500]


def _http_error_message(resp: httpx.Response) -> str:
    fallback = f"SuperOps returned HTTP {resp.status_code}"
    if resp.status_code == 400 and not resp.content.strip():
        # SuperOps answers an unknown subdomain or a bad token with an empty 400.
        return f"{fallback} with no details; check the API token and the subdomain (and region)"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, dict):
        if isinstance(payload.get("errors"), list):
            return f"{fallback}: {_graphql_message([e for e in payload['errors'] if isinstance(e, dict)])}"
        for key in ("message", "error"):
            if isinstance(payload.get(key), str) and payload[key]:
                return f"{fallback}: {payload[key][:300]}"
    return fallback
