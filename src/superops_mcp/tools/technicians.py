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
"""Technicians, technician groups and the role / team / designation lookups."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SuperOpsClient, SuperOpsError
from ._common import (
    READ_ONLY,
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

TECHNICIAN_LIST = """
query listTechnicians($input: ListInfoInput!) {
  getTechnicianList(input: $input) {
    userList {
      userId name firstName lastName email contactNumber designation businessFunction team
      reportingManager role groups
    }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

GROUP_LIST = "query listTechnicianGroups { getTechnicianGroupList { groupId name } }"

LOOKUPS = """
query technicianLookups {
  roles: getTechnicianRoleList { roleId name }
  teams: getTeamList { teamId name }
  designations: getDesignationList { designationId name }
  businessFunctions: getBusinessFunctionList { businessFunctionId name }
  groups: getTechnicianGroupList { groupId name }
}
"""


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def superops_technicians_list(
        search: Annotated[str | None, Field(description="Text in the technician's name or email.")] = None,
        teamId: Annotated[str | None, Field(description="Only technicians in this team id (see superops_technicians_lookups).")] = None,
        activeOnly: Annotated[
            bool | None, Field(description="Accepted for compatibility; SuperOps has no active flag, so it has no effect.")
        ] = None,
        page: Page = None,
        pageSize: PageSize = None,
        max: Max = None,
        cursor: Cursor = None,
        offset: Offset = None,
    ) -> str:
        """List technicians by name: email, phone, role, groups, team and manager.

        SuperOps has no active flag, ticket counts or login times for technicians.
        """

        async def action(client: SuperOpsClient) -> Any:
            number, size, start = locate(offset, *paging(page, pageSize, cursor, max))
            term = text(search)
            condition = join(
                "AND",
                [
                    join("OR", [clause("name", "contains", term), clause("email", "contains", term)]) if term else None,
                    clause("team", "includes", [text(teamId)]) if text(teamId) else None,
                ],
            )
            sort = [{"attribute": "name", "order": "ASC"}]
            data = await client.query(TECHNICIAN_LIST, {"input": list_input(number, size, condition, sort)})
            return page_result(data.get("getTechnicianList"), "userList", number, size, start)

        return await run_tool("superops_technicians_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_technicians_get(
        technicianId: Annotated[str, Field(description="Technician user id.", min_length=1)],
    ) -> str:
        """Get one technician by user id: contact details, role, groups, team, designation and manager."""

        async def action(client: SuperOpsClient) -> Any:
            # There is no single-technician query; filter the list by id.
            variables = {"input": list_input(1, 1, clause("userId", "includes", [technicianId.strip()]))}
            data = await client.query(TECHNICIAN_LIST, variables)
            users = ((data.get("getTechnicianList") or {}).get("userList")) or []
            if not users:
                raise SuperOpsError(404, f"No technician with user id {technicianId.strip()}")
            return users[0]

        return await run_tool("superops_technicians_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_technicians_groups(
        max: Annotated[int | None, Field(description="Return at most this many groups (default all).", ge=1)] = None,
    ) -> str:
        """List every technician group (id and name); member lists are not exposed by SuperOps."""

        async def action(client: SuperOpsClient) -> Any:
            groups = (await client.query(GROUP_LIST)).get("getTechnicianGroupList") or []
            total = len(groups)
            if max is not None and total > max:
                return {"groups": groups[:max], "count": max, "totalCount": total}
            return {"groups": groups, "count": total}

        return await run_tool("superops_technicians_groups", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_technicians_lookups() -> str:
        """List the tenant's roles, teams, designations, business functions and groups with their ids.

        Use it to turn a name ("the Sales team") into the id SuperOps filters on.
        """

        async def action(client: SuperOpsClient) -> Any:
            data = await client.query(LOOKUPS)
            return {key: data.get(key) or [] for key in ("roles", "teams", "designations", "businessFunctions", "groups")}

        return await run_tool("superops_technicians_lookups", client_factory, action)
