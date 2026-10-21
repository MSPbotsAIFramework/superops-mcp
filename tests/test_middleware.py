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
"""Credential middleware: 401 on missing headers, bad values reported by tools, isolation."""

import asyncio

import pytest
from starlette.testclient import TestClient

from superops_mcp.__main__ import build_http_app
from superops_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
    normalize_subdomain,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {"X-SuperOps-API-Token": "your-api-token", "X-SuperOps-Subdomain": "yourcompany"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers,missing",
    [
        ({}, "X-SuperOps-API-Token"),
        ({"X-SuperOps-API-Token": "t"}, "X-SuperOps-Subdomain"),
        ({"X-SuperOps-Subdomain": "example"}, "X-SuperOps-API-Token"),
        ({"X-SuperOps-API-Token": "Bearer ", "X-SuperOps-Subdomain": "example"}, "X-SuperOps-API-Token"),
        ({"X-SuperOps-API-Token": "${SUPEROPS_TOKEN}", "X-SuperOps-Subdomain": "example"}, "X-SuperOps-API-Token"),
    ],
)
def test_missing_credentials_return_401(headers, missing):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    body = resp.json()
    assert body["required_headers"] == ["X-SuperOps-API-Token", "X-SuperOps-Subdomain"]
    assert missing in body["message"]


def test_placeholder_headers_still_list_tools():
    # The admin console refreshes the tool catalog with placeholder header values.
    headers = {"X-SuperOps-API-Token": "placeholder", "X-SuperOps-Subdomain": "placeholder", "X-SuperOps-Region": "placeholder"}
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 200
    assert "superops_tickets_list" in resp.text


@pytest.mark.parametrize(
    "extra,message",
    [
        ({"X-SuperOps-Subdomain": "not a subdomain!"}, "X-SuperOps-Subdomain must be the tenant subdomain, e.g. yourcompany"),
        ({"X-SuperOps-Region": "apac"}, "X-SuperOps-Region must be one of: us, eu"),
    ],
)
def test_bad_values_are_reported_by_tools(extra, message):
    with _client() as client:
        listed = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | extra)
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "superops_status", "arguments": {}}}
        resp = client.post("/mcp", json=call, headers=MCP_HEADERS | CREDS | extra)
    assert listed.status_code == 200
    assert resp.status_code == 200
    assert '\\"invalid_argument\\"' in resp.text
    assert message in resp.text


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("superops_navigate", "superops_tickets_create", "superops_custom_mutation"):
        assert name in resp.text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("yourcompany", "yourcompany"),
        ("YourCompany", "yourcompany"),
        ("yourcompany.superops.ai", "yourcompany"),
        ("https://yourcompany.superops.ai/#/tickets", "yourcompany"),
        ("your company", None),
    ],
)
def test_normalize_subdomain(value, expected):
    assert normalize_subdomain(value) == expected


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def test_headers_reach_request_context_and_are_reset():
    seen = _run_middleware(
        [(b"x-superops-api-token", b"Bearer token-123"), (b"x-superops-subdomain", b" Example "), (b"x-superops-region", b"EU")]
    )
    creds = seen["credentials"]
    assert (creds.api_token, creds.subdomain, creds.region) == ("token-123", "example", "eu")
    assert seen["client"].endpoint == "https://euapi.superops.ai/msp"
    assert _credentials_var.get() is None


def test_region_defaults_to_us():
    seen = _run_middleware([(b"x-superops-api-token", b"t"), (b"x-superops-subdomain", b"example")])
    assert seen["client"].endpoint == "https://api.superops.ai/msp"


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().subdomain
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().subdomain))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(subdomain: bytes) -> dict:
        return {"type": "http", "path": "/mcp", "headers": [(b"x-superops-api-token", b"t"), (b"x-superops-subdomain", subdomain)]}

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a"), receive, send), middleware(scope(b"tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_token():
    seen = _run_middleware([(b"x-superops-api-token", b"very-secret-token"), (b"x-superops-subdomain", b"example")])
    assert "very-secret-token" not in repr(seen["credentials"])
