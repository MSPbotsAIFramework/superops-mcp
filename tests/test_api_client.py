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


# ---- compatibility aliases from the older tool contract ----------------------

GROUPS = {"getTechnicianGroupList": [{"groupId": "g1", "name": "Service Desk"}, {"groupId": "g2", "name": "Network"}]}


async def test_ticket_create_category_name_is_category(monkeypatch):
    rec = Recorder({"createTicket": {"createTicket": {"ticketId": "9"}}})
    await _call(monkeypatch, rec, "superops_tickets_create", {"subject": "s", "clientId": "c1", "categoryName": "Network"})
    assert rec.variables()["input"]["category"] == "Network"


async def test_ticket_create_category_conflict(monkeypatch):
    rec = Recorder()
    args = {"subject": "s", "clientId": "c1", "category": "Network", "categoryName": "Hardware"}
    result = await _call(monkeypatch, rec, "superops_tickets_create", args)
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_ticket_create_tech_group_name_resolves(monkeypatch):
    rec = Recorder({"getTechnicianGroupList": GROUPS, "createTicket": {"createTicket": {"ticketId": "9"}}})
    await _call(monkeypatch, rec, "superops_tickets_create", {"subject": "s", "clientId": "c1", "techGroupName": " service desk "})
    assert rec.variables(1)["input"]["techGroup"] == {"groupId": "g1"}


@pytest.mark.parametrize(
    "groups,args,expected",
    [
        (GROUPS, {"techGroupName": "Billing"}, "Network, Service Desk"),
        ({"getTechnicianGroupList": [{"groupId": "a", "name": "Ops"}, {"groupId": "b", "name": "ops"}]}, {"techGroupName": "Ops"}, "(a)"),
        (GROUPS, {"techGroupName": "Network", "techGroupId": "g1"}, "disagree"),
    ],
)
async def test_ticket_create_tech_group_name_errors(monkeypatch, groups, args, expected):
    rec = Recorder({"getTechnicianGroupList": groups, "createTicket": {"createTicket": {}}})
    result = await _call(monkeypatch, rec, "superops_tickets_create", {"subject": "s", "clientId": "c1", **args})
    assert result["error"]["code"] == "invalid_argument"
    assert expected in result["error"]["message"]
    assert len(rec.requests) == 1  # only the group lookup, no createTicket


async def test_ticket_create_requester_email_resolves(monkeypatch):
    users = {
        "getClientUserList": {
            "userList": [{"userId": "u1", "email": "pat.doe@example.com"}, {"userId": "u2", "email": "pat@example.com"}]
        }
    }
    rec = Recorder({"getClientUserList": users, "createTicket": {"createTicket": {"ticketId": "9"}}})
    await _call(monkeypatch, rec, "superops_tickets_create", {"subject": "s", "clientId": "c1", "requesterEmail": "PAT@example.com"})
    lookup = rec.variables(0)["input"]
    assert lookup["clientId"] == "c1"
    assert lookup["listInfo"]["condition"] == {"attribute": "email", "operator": "contains", "value": "PAT@example.com"}
    assert rec.variables(1)["input"]["requester"] == {"userId": "u2"}


@pytest.mark.parametrize(
    "args", [{"requesterEmail": "nobody@example.com"}, {"requesterEmail": "pat@example.com", "requesterId": "u7"}]
)
async def test_ticket_create_requester_email_errors(monkeypatch, args):
    users = {"getClientUserList": {"userList": [{"userId": "u2", "email": "pat@example.com"}]}}
    rec = Recorder({"getClientUserList": users, "createTicket": {"createTicket": {}}})
    result = await _call(monkeypatch, rec, "superops_tickets_create", {"subject": "s", "clientId": "c1", **args})
    assert result["error"]["code"] == "invalid_argument"
    assert len(rec.requests) == 1


async def test_ticket_update_assignee_id_is_technician_id(monkeypatch):
    rec = Recorder({"updateTicket": {"updateTicket": {"ticketId": "9"}}})
    await _call(monkeypatch, rec, "superops_tickets_update", {"ticketId": "9", "assigneeId": "t1"})
    assert rec.variables()["input"] == {"ticketId": "9", "technician": {"userId": "t1"}}
    args = {"ticketId": "9", "assigneeId": "t1", "technicianId": "t2"}
    conflict = await _call(monkeypatch, rec, "superops_tickets_update", args)
    assert conflict["error"]["code"] == "invalid_argument"
    assert len(rec.requests) == 1


async def test_ticket_update_tech_group_name(monkeypatch):
    rec = Recorder({"getTechnicianGroupList": GROUPS, "updateTicket": {"updateTicket": {"ticketId": "9"}}})
    await _call(monkeypatch, rec, "superops_tickets_update", {"ticketId": "9", "techGroupName": "network"})
    assert rec.variables(1)["input"] == {"ticketId": "9", "techGroup": {"groupId": "g2"}}


async def test_ticket_update_resolution_adds_private_note(monkeypatch):
    rec = Recorder({"updateTicket": {"updateTicket": {"ticketId": "9"}}, "createNote": {"createNote": {"noteId": "n1"}}})
    args = {"ticketId": "9", "status": "Resolved", "resolution": "Replaced cable"}
    result = await _call(monkeypatch, rec, "superops_tickets_update", args)
    assert rec.variables(0)["input"] == {"ticketId": "9", "status": "Resolved"}
    assert rec.variables(1)["input"] == {
        "workItem": {"workId": "9", "module": "TICKET"},
        "content": "Replaced cable",
        "privacyType": "PRIVATE",
    }
    assert result == {"ticket": {"ticketId": "9"}, "resolutionNote": {"noteId": "n1"}}


async def test_ticket_update_resolution_only(monkeypatch):
    rec = Recorder({"createNote": {"createNote": {"noteId": "n1"}}})
    result = await _call(monkeypatch, rec, "superops_tickets_update", {"ticketId": "9", "resolution": "Done"})
    assert len(rec.requests) == 1 and "createNote" in json.loads(rec.requests[0].content)["query"]
    assert result == {"ticket": None, "resolutionNote": {"noteId": "n1"}}


async def test_ticket_update_reports_failed_resolution_note(monkeypatch):
    rec = Recorder({"updateTicket": {"updateTicket": {"ticketId": "9"}}, "createNote": lambda r: httpx.Response(403)})
    args = {"ticketId": "9", "status": "Resolved", "resolution": "Done"}
    result = await _call(monkeypatch, rec, "superops_tickets_update", args)
    assert result["ticket"] == {"ticketId": "9"} and result["resolutionNote"] is None
    assert result["resolutionNoteError"]["code"] == "unauthorized"


async def test_log_time_duration_minutes_become_hours(monkeypatch):
    rec = Recorder({"createWorklogEntries": {"createWorklogEntries": [{"itemId": "w1"}]}})
    await _call(monkeypatch, rec, "superops_tickets_log_time", {"ticketId": "9", "duration": 90, "description": "Fixed VPN"})
    [entry] = rec.variables()["input"]
    assert entry["qty"] == "1.5" and entry["notes"] == "Fixed VPN"
    await _call(monkeypatch, rec, "superops_tickets_log_time", {"ticketId": "9", "duration": 20})
    assert rec.variables(1)["input"][0]["qty"] == "0.3333"


@pytest.mark.parametrize(
    "args",
    [
        {"ticketId": "9"},
        {"ticketId": "9", "qty": "1", "duration": 60},
        {"ticketId": "9", "qty": "1", "notes": "a", "description": "b"},
        {"ticketId": "9", "duration": 0.001},
    ],
)
async def test_log_time_argument_errors(monkeypatch, args):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "superops_tickets_log_time", args)
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_log_time_work_type_resolves_service_item(monkeypatch):
    items = {
        "getServiceCatalogItemList": {
            "items": [
                {"itemId": "s1", "name": "Remote Support", "offerAsWorklogItem": False},
                {"itemId": "s2", "name": "Remote Support", "offerAsWorklogItem": True},
                {"itemId": "s3", "name": "Remote Support After Hours", "offerAsWorklogItem": True},
            ]
        }
    }
    rec = Recorder({"getServiceCatalogItemList": items, "createWorklogEntries": {"createWorklogEntries": []}})
    await _call(monkeypatch, rec, "superops_tickets_log_time", {"ticketId": "9", "qty": "1", "workType": "remote support"})
    assert rec.variables(0)["input"]["condition"] == {"attribute": "name", "operator": "contains", "value": "remote support"}
    assert rec.variables(1)["input"][0]["serviceItem"] == {"itemId": "s2"}


async def test_log_time_unknown_work_type(monkeypatch):
    items = {"getServiceCatalogItemList": {"items": [{"itemId": "s3", "name": "On-site Visit"}]}}
    rec = Recorder({"getServiceCatalogItemList": items, "createWorklogEntries": {"createWorklogEntries": []}})
    args = {"ticketId": "9", "duration": 30, "workType": "On-site"}
    result = await _call(monkeypatch, rec, "superops_tickets_log_time", args)
    assert result["error"]["code"] == "invalid_argument"
    assert "service catalog item" in result["error"]["message"]
    assert len(rec.requests) == 1


async def test_asset_patches_status_alias(monkeypatch):
    rec = Recorder({"getAssetPatchDetails": {"getAssetPatchDetails": {}}})
    await _call(monkeypatch, rec, "superops_assets_patches", {"assetId": "a1", "status": "Installed"})
    condition = rec.variables()["input"]["listInfo"]["condition"]
    assert condition == {"attribute": "installationStatus", "operator": "includes", "value": ["Installed"]}
    args = {"assetId": "a1", "status": "Installed", "installationStatus": "Failed"}
    conflict = await _call(monkeypatch, rec, "superops_assets_patches", args)
    assert conflict["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize(
    "tool,args,path",
    [
        ("superops_assets_software", {"assetId": "a1", "max": 20}, ("input", "listInfo")),
        ("superops_clients_search", {"query": "x", "max": 20}, ("input",)),
        ("superops_clients_list", {"max": 20}, ("input",)),
        ("superops_assets_list", {"max": 20}, ("input",)),
        ("superops_tickets_list", {"max": 20}, ("input",)),
        ("superops_technicians_list", {"max": 20}, ("input",)),
    ],
)
async def test_max_is_page_size(monkeypatch, tool, args, path):
    rec = Recorder()
    await _call(monkeypatch, rec, tool, args)
    listing = rec.variables()
    for key in path:
        listing = listing[key]
    assert listing["pageSize"] == 20


async def test_max_is_capped_and_cursor_is_page(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "superops_clients_list", {"max": 500, "cursor": "3"})
    assert rec.variables()["input"]["pageSize"] == 100 and rec.variables()["input"]["page"] == 3


@pytest.mark.parametrize("args", [{"cursor": "abc"}, {"cursor": "2", "page": 3}, {"max": 10, "pageSize": 20}])
async def test_paging_alias_errors(monkeypatch, args):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "superops_tickets_list", args)
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_tickets_list_assignee_and_unassigned(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "superops_tickets_list", {"assigneeId": "t1"})
    assert rec.variables(0)["input"]["condition"] == {"attribute": "technician", "operator": "includes", "value": ["t1"]}
    await _call(monkeypatch, rec, "superops_tickets_list", {"unassigned": True})
    assert rec.variables(1)["input"]["condition"] == {"attribute": "technician", "operator": "isEmpty"}
    both = await _call(monkeypatch, rec, "superops_tickets_list", {"unassigned": True, "assigneeId": "t1"})
    assert both["error"]["code"] == "invalid_argument"
    assert len(rec.requests) == 2


async def test_technicians_list_team_and_active_only(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "superops_technicians_list", {"teamId": "tm1", "activeOnly": True})
    assert rec.variables()["input"]["condition"] == {"attribute": "team", "operator": "includes", "value": ["tm1"]}


async def test_technician_groups_max_limits_locally(monkeypatch):
    rec = Recorder({"getTechnicianGroupList": GROUPS})
    result = await _call(monkeypatch, rec, "superops_technicians_groups", {"max": 1})
    assert result == {"groups": [{"groupId": "g1", "name": "Service Desk"}], "count": 1, "totalCount": 2}


async def test_custom_query_missing_query_is_invalid(monkeypatch):
    rec = Recorder()
    for args in ({}, {"query": "  "}):
        result = await _call(monkeypatch, rec, "superops_custom_query", args)
        assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


# ---- page-number paging that survives truncation (offset / next_offset) ------

# (tool, base args, GraphQL root field, list field, path to the ListInfoInput in the variables)
PAGED_TOOLS = [
    ("superops_clients_search", {"query": "acme"}, "getClientList", "clients", ("input",)),
    ("superops_assets_software", {"assetId": "a1"}, "getAssetSoftwareList", "assetSoftwares", ("input", "listInfo")),
    ("superops_assets_patches", {"assetId": "a1"}, "getAssetPatchDetails", "assetPatches", ("input", "listInfo")),
]


def _big_records(n: int) -> list[dict]:
    # 40 fields of 80 characters: nothing to shorten, so only a few records fit one response.
    return [{"id": i, **{f"f{k}": f"{i:04d}" + "x" * 76 for k in range(40)}} for i in range(n)]


def _small_records(n: int) -> list[dict]:
    return [{"id": i} for i in range(n)]


def _list_info(rec: Recorder, path: tuple, index: int = -1) -> dict:
    listing = json.loads(rec.requests[index].content)["variables"]
    for key in path:
        listing = listing[key]
    return listing


def _paged_api(root: str, field: str, path: tuple, records: list[dict]):
    """SuperOps pages by page (from 1) and pageSize only; hasMore is null on the last page."""

    def reply(request: httpx.Request) -> httpx.Response:
        listing = json.loads(request.content)["variables"]
        for key in path:
            listing = listing[key]
        number, size = listing["page"], listing["pageSize"]
        data = records[(number - 1) * size : number * size]
        more = number * size < len(records) or None
        info = {"page": number, "pageSize": size, "totalCount": len(records), "hasMore": more}
        return httpx.Response(200, json={"data": {root: {field: data, "listInfo": info}}})

    return reply


def _paged_recorder(root, field, path, records) -> Recorder:
    return Recorder({root: _paged_api(root, field, path, records)})


async def _walk(monkeypatch, tool, args, root, field, path, records, first) -> tuple[list[int], list[dict]]:
    rec = _paged_recorder(root, field, path, records)
    results = [await _call(monkeypatch, rec, tool, {**args, **first})]
    while results[-1].get("next_offset") is not None:
        assert len(results) < 50
        results.append(await _call(monkeypatch, rec, tool, {**args, "pageSize": first["pageSize"], "offset": results[-1]["next_offset"]}))
    return [r["id"] for res in results for r in res[field]], results


@pytest.mark.parametrize("tool,args,root,field,path", PAGED_TOOLS)
async def test_cut_pages_walked_by_next_offset_return_every_record_once(monkeypatch, tool, args, root, field, path):
    ids, results = await _walk(monkeypatch, tool, args, root, field, path, _big_records(35), {"page": 1, "pageSize": 10})
    assert ids == list(range(35))
    assert any(r.get("truncated") for r in results)
    for r in results:
        if r.get("truncated"):
            assert "retry_page_size" not in r and r["next_offset"] == r["offset"] + len(r[field])
    assert results[-1]["next_offset"] is None


@pytest.mark.parametrize("tool,args,root,field,path", PAGED_TOOLS)
async def test_cut_pages_walked_from_a_later_page(monkeypatch, tool, args, root, field, path):
    ids, _ = await _walk(monkeypatch, tool, args, root, field, path, _big_records(35), {"page": 3, "pageSize": 10})
    assert ids == list(range(20, 35))


@pytest.mark.parametrize("tool,args,root,field,path", PAGED_TOOLS)
@pytest.mark.parametrize(
    "offset,page_sent,expected",
    [(13, 2, list(range(13, 20))), (10, 2, list(range(10, 20))), (0, 1, list(range(10))), (24, 3, [24])],
)
async def test_offset_reads_the_page_that_holds_it(monkeypatch, tool, args, root, field, path, offset, page_sent, expected):
    rec = _paged_recorder(root, field, path, _small_records(25))
    result = await _call(monkeypatch, rec, tool, {**args, "pageSize": 10, "offset": offset})
    assert _list_info(rec, path)["page"] == page_sent and _list_info(rec, path)["pageSize"] == 10
    assert [r["id"] for r in result[field]] == expected
    assert result["offset"] == offset
    assert result["next_offset"] == (offset + len(expected) if offset + len(expected) < 25 else None)


@pytest.mark.parametrize("tool,args,root,field,path", PAGED_TOOLS)
async def test_offset_with_default_page_size(monkeypatch, tool, args, root, field, path):
    rec = _paged_recorder(root, field, path, _small_records(120))
    result = await _call(monkeypatch, rec, tool, {**args, "offset": 75})
    assert _list_info(rec, path)["page"] == 2 and _list_info(rec, path)["pageSize"] == 50
    assert [r["id"] for r in result[field]] == list(range(75, 100)) and result["next_offset"] == 100


@pytest.mark.parametrize("tool,args,root,field,path", PAGED_TOOLS)
async def test_offset_past_the_end(monkeypatch, tool, args, root, field, path):
    rec = _paged_recorder(root, field, path, _small_records(25))
    result = await _call(monkeypatch, rec, tool, {**args, "pageSize": 10, "offset": 40})
    assert result[field] == [] and not result["listInfo"]["hasMore"]
    assert result["next_offset"] is None


@pytest.mark.parametrize("tool,args,root,field,path", PAGED_TOOLS)
async def test_offset_wins_over_page(monkeypatch, tool, args, root, field, path):
    rec = _paged_recorder(root, field, path, _small_records(25))
    result = await _call(monkeypatch, rec, tool, {**args, "page": 1, "pageSize": 10, "offset": 15})
    assert _list_info(rec, path)["page"] == 2
    assert [r["id"] for r in result[field]] == list(range(15, 20))


async def test_offset_with_max_as_page_size(monkeypatch):
    root, field, path = "getAssetSoftwareList", "assetSoftwares", ("input", "listInfo")
    rec = _paged_recorder(root, field, path, _small_records(60))
    result = await _call(monkeypatch, rec, "superops_assets_software", {"assetId": "a1", "max": 20, "offset": 45})
    assert _list_info(rec, path)["page"] == 3 and _list_info(rec, path)["pageSize"] == 20
    assert [r["id"] for r in result[field]] == list(range(45, 60)) and result["next_offset"] is None


@pytest.mark.parametrize("tool,args,root,field,path", PAGED_TOOLS)
@pytest.mark.parametrize("paging", [{}, {"page": 2}, {"page": 2, "pageSize": 10}])
async def test_page_only_call_is_unchanged(monkeypatch, tool, args, root, field, path, paging):
    rec = _paged_recorder(root, field, path, _small_records(125))
    result = await _call(monkeypatch, rec, tool, {**args, **paging})
    listing = _list_info(rec, path)
    size, number = paging.get("pageSize", 50), paging.get("page", 1)
    assert {k: listing[k] for k in ("page", "pageSize")} == {"page": number, "pageSize": size}
    assert set(listing) <= {"page", "pageSize", "condition", "sort"}
    assert [r["id"] for r in result[field]] == list(range((number - 1) * size, min(number * size, 125)))
    assert result["listInfo"] == {"page": number, "pageSize": size, "totalCount": 125, "hasMore": True}
