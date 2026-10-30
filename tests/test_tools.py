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
"""tools/list snapshot, GraphQL document syntax and error-envelope mapping. No network access."""

import asyncio
import importlib
import json

import pytest
from graphql import parse

from superops_mcp.api_client import SuperOpsError, graphql_error_status
from superops_mcp.server import INSTRUCTIONS, create_mcp_server

# Tool names are a public contract: existing agents and tenant connections
# depend on them. Changing this snapshot is a breaking change.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "superops_navigate": {"domain"},
    "superops_automation_scripts_list": set(),
    "superops_automation_scripts_run": {"scriptId", "assetId"},
    "superops_automation_alerts_list": set(),
    "superops_automation_alerts_resolve": {"alertIds"},
    "superops_status": set(),
    "superops_test_connection": set(),
    "superops_clients_list": set(),
    "superops_clients_get": {"accountId"},
    "superops_clients_search": {"query"},
    "superops_tickets_list": set(),
    "superops_tickets_get": {"ticketId"},
    "superops_tickets_create": {"subject", "clientId"},
    "superops_tickets_update": {"ticketId"},
    "superops_tickets_add_note": {"ticketId", "content"},
    "superops_tickets_log_time": {"ticketId"},
    "superops_assets_list": set(),
    "superops_assets_get": {"assetId"},
    "superops_assets_software": {"assetId"},
    "superops_assets_patches": {"assetId"},
    "superops_technicians_list": set(),
    "superops_technicians_get": {"technicianId"},
    "superops_technicians_groups": set(),
    "superops_technicians_lookups": set(),
    "superops_custom_query": set(),
    "superops_custom_mutation": {"mutation"},
}

WRITE_TOOLS = {
    "superops_tickets_create",
    "superops_tickets_update",
    "superops_tickets_add_note",
    "superops_tickets_log_time",
    "superops_custom_mutation",
    "superops_automation_scripts_run",
    "superops_automation_alerts_resolve",
}

# Destructive-capable (annotations.destructiveHint): a mutation that can run arbitrary vendor-side
# commands or logic, not just change one record the agent named explicitly.
DESTRUCTIVE_TOOLS = {"superops_custom_mutation", "superops_automation_scripts_run"}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tool_names_are_stable(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_annotations_match_side_effects(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is (name not in WRITE_TOOLS)
    assert bool(annotations.destructiveHint) is (name in DESTRUCTIVE_TOOLS)


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_write_tools_say_when_to_use_them(tools, name):
    assert "explicitly asks" in (tools[name].description or "")


def test_page_size_is_capped(tools):
    schema = tools["superops_tickets_list"].inputSchema["properties"]["pageSize"]
    assert next(a["maximum"] for a in schema["anyOf"] if "maximum" in a) == 100


@pytest.mark.parametrize("module", ["navigation", "clients", "tickets", "assets", "technicians", "automation"])
def test_graphql_documents_parse(module):
    mod = importlib.import_module(f"superops_mcp.tools.{module}")
    documents = [v for v in vars(mod).values() if isinstance(v, str) and v.lstrip().startswith(("query", "mutation"))]
    assert documents
    for document in documents:
        parse(document)


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (400, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
    ],
)
def test_error_envelope(status, code, retryable):
    envelope = json.loads(SuperOpsError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


@pytest.mark.parametrize(
    "errors,expected",
    [
        ([{"message": "Unauthorized access"}], (401, None)),
        ([{"message": "x", "extensions": {"code": "UNAUTHENTICATED"}}], (401, None)),
        ([{"message": "You do not have permission"}], (403, None)),
        ([{"message": "Ticket not found"}], (404, None)),
        ([{"message": "slow down", "extensions": {"retryAfter": 3}}], (429, 3.0)),
        ([{"message": "Validation error of type FieldUndefined"}], (400, None)),
    ],
)
def test_graphql_error_mapping(errors, expected):
    assert graphql_error_status(errors) == expected


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("superops_test_connection", {}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"


def test_navigate_needs_no_credentials():
    content, _ = asyncio.run(create_mcp_server().call_tool("superops_navigate", {"domain": "custom"}))
    assert json.loads(content[0].text)["tools"] == ["superops_custom_query", "superops_custom_mutation"]


def test_navigate_describes_automation_domain():
    """TC1 (AC1, PRD-21205): superops_navigate(domain="automation") names the four new tools,
    with no upstream call (navigate needs no credentials, same as every other domain)."""
    content, _ = asyncio.run(create_mcp_server().call_tool("superops_navigate", {"domain": "automation"}))
    body = json.loads(content[0].text)
    assert body["tools"] == [
        "superops_automation_scripts_list",
        "superops_automation_scripts_run",
        "superops_automation_alerts_list",
        "superops_automation_alerts_resolve",
    ]
    assert body["summary"]
