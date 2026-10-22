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
"""Clients: the MSP's customer accounts."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SuperOpsClient
from ._common import (
    READ_ONLY,
    TENANT_VALUES,
    ClientFactory,
    Cursor,
    Max,
    Offset,
    Page,
    PageSize,
    clause,
    join,
    list_input,
    locate,
    page_result,
    paging,
    run_tool,
    text,
)

CLIENT_LIST = """
query listClients($input: ListInfoInput!) {
  getClientList(input: $input) {
    clients { accountId name stage status emailDomains accountManager primaryContact hqSite updatedTime }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

CLIENT_GET = """
query getClient($input: ClientIdentifierInput!) {
  getClient(input: $input) {
    accountId name stage status emailDomains accountManager primaryContact secondaryContact
    hqSite technicianGroups customFields createdTime updatedTime
  }
}
"""

BY_NAME = [{"attribute": "name", "order": "ASC"}]


def name_or_domain(term: str) -> dict | None:
    return join("OR", [clause("name", "contains", term), clause("emailDomains", "contains", term)])


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def superops_clients_list(
        status: Annotated[
            str | None,
            Field(
                description="Status within the stage: Paid / Unpaid (stage Active), New / Negotiation / Won / Lost "
                f"(stage Prospect). {TENANT_VALUES}"
            ),
        ] = None,
        stage: Annotated[str | None, Field(description=f"Stage: Active, Inactive or Prospect. {TENANT_VALUES}")] = None,
        page: Page = None,
        pageSize: PageSize = None,
        max: Max = None,
        cursor: Cursor = None,
        offset: Offset = None,
    ) -> str:
        """List client accounts by name, optionally by stage and status, with contacts and HQ site."""

        async def action(client: SuperOpsClient) -> Any:
            number, size, start = locate(offset, *paging(page, pageSize, cursor, max))
            condition = join(
                "AND",
                [
                    clause("stage", "includes", [text(stage)]) if text(stage) else None,
                    clause("status", "includes", [text(status)]) if text(status) else None,
                ],
            )
            data = await client.query(CLIENT_LIST, {"input": list_input(number, size, condition, BY_NAME)})
            return page_result(data.get("getClientList"), "clients", number, size, start)

        return await run_tool("superops_clients_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_clients_get(accountId: Annotated[str, Field(description="Client account id.", min_length=1)]) -> str:
        """Get one client account: stage, status, contacts, HQ site, technician groups and custom fields."""

        async def action(client: SuperOpsClient) -> Any:
            data = await client.query(CLIENT_GET, {"input": {"accountId": accountId.strip()}})
            return data.get("getClient")

        return await run_tool("superops_clients_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_clients_search(
        query: Annotated[str, Field(description="Text in the client name or any of its email domains.", min_length=1)],
        page: Page = None,
        pageSize: PageSize = None,
        max: Max = None,
        offset: Offset = None,
    ) -> str:
        """Search client accounts by part of their name or email domain."""

        async def action(client: SuperOpsClient) -> Any:
            term = text(query)
            if not term:
                raise ValueError("query must not be blank.")
            number, size, start = locate(offset, *paging(page, pageSize, None, max))
            data = await client.query(CLIENT_LIST, {"input": list_input(number, size, name_or_domain(term), BY_NAME)})
            return page_result(data.get("getClientList"), "clients", number, size, start)

        return await run_tool("superops_clients_search", client_factory, action)
