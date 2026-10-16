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
"""Tool discovery (kept for compatibility), status and the connection test."""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .._json import compact as to_json
from ..api_client import SuperOpsClient
from ._common import LOCAL, READ_ONLY, ClientFactory, run_tool

DOMAINS: dict[str, tuple[str, list[str]]] = {
    "clients": (
        "Client accounts: list, get, search by name or email domain.",
        ["superops_clients_list", "superops_clients_get", "superops_clients_search"],
    ),
    "tickets": (
        "Tickets: list, get, create, update, add notes, log time.",
        [
            "superops_tickets_list",
            "superops_tickets_get",
            "superops_tickets_create",
            "superops_tickets_update",
            "superops_tickets_add_note",
            "superops_tickets_log_time",
        ],
    ),
    "assets": (
        "Managed assets (endpoints): list, get, software inventory, patches.",
        ["superops_assets_list", "superops_assets_get", "superops_assets_software", "superops_assets_patches"],
    ),
    "technicians": (
        "Technicians, technician groups and the role / team / designation lookups.",
        [
            "superops_technicians_list",
            "superops_technicians_get",
            "superops_technicians_groups",
            "superops_technicians_lookups",
        ],
    ),
    "custom": (
        "Raw GraphQL: read-only queries, and mutations for writes no other tool covers.",
        ["superops_custom_query", "superops_custom_mutation"],
    ),
}

_PING = "query ping($input: ListInfoInput!) { getClientList(input: $input) { listInfo { totalCount } } }"


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=LOCAL)
    async def superops_navigate(
        domain: Annotated[
            Literal["clients", "tickets", "assets", "technicians", "custom"], Field(description="Area to describe.")
        ],
    ) -> str:
        """Describe the tools of one SuperOps area. All tools are already callable; this changes nothing."""
        summary, tools = DOMAINS[domain]
        return to_json({"domain": domain, "summary": summary, "tools": tools})

    @mcp.tool(annotations=LOCAL)
    async def superops_status() -> str:
        """Show which tenant and region this connection targets and the tool areas (no API call)."""

        async def action(client: SuperOpsClient) -> dict:
            return {
                "configured": True,
                "subdomain": client.subdomain,
                "region": client.region,
                "endpoint": client.endpoint,
                "domains": {name: summary for name, (summary, _tools) in DOMAINS.items()},
            }

        return await run_tool("superops_status", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_test_connection() -> str:
        """Test the API token and subdomain with a minimal query; returns the tenant's client count."""

        async def action(client: SuperOpsClient) -> dict:
            data = await client.query(_PING, {"input": {"page": 1, "pageSize": 1}})
            info = ((data.get("getClientList") or {}).get("listInfo")) or {}
            return {"connected": True, "subdomain": client.subdomain, "region": client.region, "client_count": info.get("totalCount")}

        return await run_tool("superops_test_connection", client_factory, action)
