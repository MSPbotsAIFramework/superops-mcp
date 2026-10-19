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
"""Headers, retries, GraphQL errors and tool variables against a mocked SuperOps API."""

import json

import httpx
import pytest

from superops_mcp import api_client
from superops_mcp.api_client import SuperOpsClient, SuperOpsCredentials, SuperOpsError
from superops_mcp.server import _credentials_var, create_mcp_server

CREDS = SuperOpsCredentials(api_token="token-123", subdomain="example")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


class Recorder:
    """Mock GraphQL endpoint: replies by the root field of the operation."""

    def __init__(self, routes=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        query = json.loads(request.content)["query"]
        for key, reply in self.routes.items():
            if key in query:
                return reply(request) if callable(reply) else httpx.Response(200, json={"data": reply})
        return httpx.Response(200, json={"data": {}})

    def variables(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)["variables"]


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder) -> SuperOpsClient:
    return SuperOpsClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- client ----------------------------------------------------------------


async def test_headers_and_endpoint():
    rec = Recorder({"getClientList": {"getClientList": {}}})
    await _client(rec).query("query { getClientList }")
    request = rec.requests[0]
    assert str(request.url) == "https://api.superops.ai/msp"
    assert request.headers["Authorization"] == "Bearer token-123"
    assert request.headers["CustomerSubDomain"] == "example"


async def test_graphql_errors_with_http_200_raise():
    reply = httpx.Response(200, json={"errors": [{"message": "Invalid token"}], "data": None})
    rec = Recorder({"getClientList": lambda r: reply})
    with pytest.raises(SuperOpsError) as err:
        await _client(rec).query("query { getClientList }")
    assert err.value.status_code == 401
    assert len(rec.requests) == 1


async def test_query_retries_rate_limit_from_extensions():
    replies = iter(
        [
            httpx.Response(200, json={"errors": [{"message": "Too many requests", "extensions": {"retryAfter": 1}}]}),
            httpx.Response(200, json={"data": {"getClientList": {"clients": []}}}),
        ]
    )
    rec = Recorder({"getClientList": lambda r: next(replies)})
    assert await _client(rec).query("query { getClientList }") == {"getClientList": {"clients": []}}
    assert len(rec.requests) == 2


async def test_query_retries_5xx_but_mutation_does_not():
    rec = Recorder({"createTicket": lambda r: httpx.Response(502), "getTicket": lambda r: httpx.Response(502)})
    with pytest.raises(SuperOpsError):
        await _client(rec).query("mutation { createTicket }", operation="mutation")
    assert len(rec.requests) == 1
    with pytest.raises(SuperOpsError):
        await _client(rec).query("query { getTicket }")
    assert len(rec.requests) == 1 + 3


async def test_http_401_is_unauthorized(monkeypatch):
    rec = Recorder({"getClientList": lambda r: httpx.Response(401, json={"message": "Unauthorized"})})
    result = await _call(monkeypatch, rec, "superops_test_connection", {})
    assert result["error"]["code"] == "unauthorized"
    assert "token-123" not in json.dumps(result)


async def test_test_connection(monkeypatch):
    rec = Recorder({"getClientList": {"getClientList": {"listInfo": {"totalCount": 42}}}})
    result = await _call(monkeypatch, rec, "superops_test_connection", {})
    assert result == {"connected": True, "subdomain": "example", "region": "us", "client_count": 42}


# ---- clients ---------------------------------------------------------------


async def test_clients_list_condition(monkeypatch):
    rec = Recorder({"getClientList": {"getClientList": {"clients": [], "listInfo": {}}}})
    await _call(monkeypatch, rec, "superops_clients_list", {"stage": "Active", "status": "Paid", "pageSize": 10})
    assert rec.variables()["input"] == {
        "page": 1,
        "pageSize": 10,
        "condition": {
            "joinOperator": "AND",
            "operands": [
                {"attribute": "stage", "operator": "includes", "value": ["Active"]},
                {"attribute": "status", "operator": "includes", "value": ["Paid"]},
            ],
        },
        "sort": [{"attribute": "name", "order": "ASC"}],
    }


async def test_clients_list_without_filters_has_no_condition(monkeypatch):
    rec = Recorder({"getClientList": {"getClientList": {}}})
    await _call(monkeypatch, rec, "superops_clients_list", {})
    assert "condition" not in rec.variables()["input"]


async def test_clients_search_matches_name_or_domain(monkeypatch):
    rec = Recorder({"getClientList": {"getClientList": {}}})
    await _call(monkeypatch, rec, "superops_clients_search", {"query": " example.com "})
    condition = rec.variables()["input"]["condition"]
    assert condition == {
        "joinOperator": "OR",
        "operands": [
            {"attribute": "name", "operator": "contains", "value": "example.com"},
            {"attribute": "emailDomains", "operator": "contains", "value": "example.com"},
        ],
    }


# ---- tickets ---------------------------------------------------------------


async def test_tickets_list_filters(monkeypatch):
    rec = Recorder({"getTicketList": {"getTicketList": {"tickets": []}}})
    args = {"status": ["Open", " "], "priority": ["High"], "clientId": "c1", "technicianId": "t1", "page": 2}
    await _call(monkeypatch, rec, "superops_tickets_list", args)
    data = rec.variables()["input"]
    assert data["page"] == 2 and data["pageSize"] == 50
    assert data["sort"] == [{"attribute": "createdTime", "order": "DESC"}]
    assert data["condition"]["operands"] == [
        {"attribute": "status", "operator": "includes", "value": ["Open"]},
        {"attribute": "priority", "operator": "includes", "value": ["High"]},
        {"attribute": "client", "operator": "includes", "value": ["c1"]},
        {"attribute": "technician", "operator": "includes", "value": ["t1"]},
    ]


async def test_ticket_create_variables(monkeypatch):
    rec = Recorder({"createTicket": {"createTicket": {"ticketId": "9"}}})
    args = {"subject": "Printer down", "clientId": "c1", "priority": "High", "technicianId": "t1", "siteId": "s1"}
    result = await _call(monkeypatch, rec, "superops_tickets_create", args)
    assert rec.variables()["input"] == {
        "subject": "Printer down",
        "client": {"accountId": "c1"},
        "source": "INTEGRATION",
        "priority": "High",
        "technician": {"userId": "t1"},
        "site": {"id": "s1"},
    }
    assert result == {"ticketId": "9"}


async def test_ticket_update_requires_a_change(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "superops_tickets_update", {"ticketId": "9"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_ticket_update_variables(monkeypatch):
    rec = Recorder({"updateTicket": {"updateTicket": {"ticketId": "9"}}})
    await _call(monkeypatch, rec, "superops_tickets_update", {"ticketId": "9", "status": "Resolved", "techGroupId": "g1"})
    assert rec.variables()["input"] == {"ticketId": "9", "status": "Resolved", "techGroup": {"groupId": "g1"}}


async def test_add_note_defaults_to_private(monkeypatch):
    rec = Recorder({"createNote": {"createNote": {"noteId": "n1"}}})
    await _call(monkeypatch, rec, "superops_tickets_add_note", {"ticketId": "9", "content": "Called the user"})
    assert rec.variables()["input"] == {
        "workItem": {"workId": "9", "module": "TICKET"},
        "content": "Called the user",
        "privacyType": "PRIVATE",
    }


async def test_log_time_accepts_numbers_and_defaults(monkeypatch):
    rec = Recorder({"createWorklogEntries": {"createWorklogEntries": [{"itemId": "w1"}]}})
    await _call(monkeypatch, rec, "superops_tickets_log_time", {"ticketId": "9", "qty": 1.5, "unitPrice": 120})
    [entry] = rec.variables()["input"]
    assert entry["qty"] == "1.5" and entry["unitPrice"] == "120"
    assert entry["billable"] is True and entry["afterHours"] is False
    assert entry["billDateTime"].endswith("Z")
    assert entry["workItem"] == {"workId": "9", "module": "TICKET"}


# ---- assets / technicians ----------------------------------------------------


async def test_asset_software_search(monkeypatch):
    rec = Recorder({"getAssetSoftwareList": {"getAssetSoftwareList": {"assetSoftwares": []}}})
    await _call(monkeypatch, rec, "superops_assets_software", {"assetId": "a1", "search": "Chrome"})
    assert rec.variables()["input"] == {
        "assetId": "a1",
        "listInfo": {"page": 1, "pageSize": 50, "condition": {"attribute": "software.name", "operator": "contains", "value": "Chrome"}},
    }


async def test_asset_patches_filters(monkeypatch):
    rec = Recorder({"getAssetPatchDetails": {"getAssetPatchDetails": {}}})
    await _call(monkeypatch, rec, "superops_assets_patches", {"assetId": "a1", "installationStatus": "NewOrMissing", "severity": ["Recommended"]})
    assert rec.variables()["input"]["listInfo"]["condition"]["operands"] == [
        {"attribute": "installationStatus", "operator": "includes", "value": ["NewOrMissing"]},
        {"attribute": "severity", "operator": "includes", "value": ["Recommended"]},
    ]


async def test_technician_get_missing_is_not_found(monkeypatch):
    rec = Recorder({"getTechnicianList": {"getTechnicianList": {"userList": []}}})
    result = await _call(monkeypatch, rec, "superops_technicians_get", {"technicianId": "u9"})
    assert result["error"]["code"] == "not_found"
    assert rec.variables()["input"]["condition"] == {"attribute": "userId", "operator": "includes", "value": ["u9"]}


async def test_technician_lookups(monkeypatch):
    rec = Recorder({"getTechnicianRoleList": {"roles": [{"roleId": "1", "name": "Admin"}], "teams": None, "groups": []}})
    result = await _call(monkeypatch, rec, "superops_technicians_lookups", {})
    assert result == {"roles": [{"roleId": "1", "name": "Admin"}], "teams": [], "designations": [], "businessFunctions": [], "groups": []}


# ---- custom ----------------------------------------------------------------


async def test_custom_query_runs_queries(monkeypatch):
    rec = Recorder({"getAssetSummary": {"getAssetSummary": {"cpu": 4}}})
    result = await _call(monkeypatch, rec, "superops_custom_query", {"query": "query { getAssetSummary { cpu } }"})
    assert result == {"getAssetSummary": {"cpu": 4}}


@pytest.mark.parametrize(
    "document",
    ["mutation { deleteTicket(input: {ticketId: 1}) }", "query a { x } mutation b { y }", "{ broken"],
)
async def test_custom_query_refuses_writes_and_bad_syntax(monkeypatch, document):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "superops_custom_query", {"query": document})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_custom_mutation_needs_a_mutation_and_is_not_retried(monkeypatch):
    rec = Recorder({"updateClient": lambda r: httpx.Response(503)})
    result = await _call(monkeypatch, rec, "superops_custom_mutation", {"mutation": "mutation { updateClient }"})
    assert result["error"]["code"] == "upstream_error"
    assert len(rec.requests) == 1
    refused = await _call(monkeypatch, rec, "superops_custom_mutation", {"mutation": "query { getClient }"})
    assert refused["error"]["code"] == "invalid_argument"


async def test_empty_400_explains_likely_cause(monkeypatch):
    rec = Recorder({"getClientList": lambda r: httpx.Response(400, content=b"")})
    result = await _call(monkeypatch, rec, "superops_test_connection", {})
    assert result["error"]["code"] == "invalid_argument"
    assert "subdomain" in result["error"]["message"]
