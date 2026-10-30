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
"""Automation: scripts that can run on a managed asset, and alerts."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SuperOpsClient, SuperOpsError
from ._common import (
    ANY_WRITE,
    READ_ONLY,
    WRITES_IDEMPOTENT,
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

# The operation names and the input/return *type* names below (ListInfoInput, ScriptListByTypeInput,
# RunScriptInput, AssetDetailsListInput, ResolveAlertInput) are confirmed from SuperOps's public
# GraphQL reference (developer.superops.com/msp/). The leaf field selections (script/alert fields
# beyond id/name) are not independently verified against a live tenant schema -- they follow this
# repo's existing conventions (assets.py's AssetDetailsListInput usage for the asset-scoped shape)
# and the field names docs/tickets/PRD-21205.md names as the best current estimate. Confirm against
# a real tenant before relying on any field here beyond id/name (see that doc's "Open questions").

SCRIPT_LIST = """
query listScripts($input: ListInfoInput!) {
  getScriptList(input: $input) {
    scripts { id name type description }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

SCRIPT_LIST_BY_TYPE = """
query listScriptsByType($input: ScriptListByTypeInput!) {
  getScriptListByType(input: $input) {
    scripts { id name type description }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

SCRIPT_RUN = """
mutation runScript($input: RunScriptInput) {
  runScriptOnAsset(input: $input) {
    runId
    status
  }
}
"""

ALERT_LIST = """
query listAlerts($input: ListInfoInput!) {
  getAlertList(input: $input) {
    alerts { id title status severity assetId createdTime }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

ALERT_LIST_FOR_ASSET = """
query listAlertsForAsset($input: AssetDetailsListInput!) {
  getAlertsForAsset(input: $input) {
    alerts { id title status severity assetId createdTime }
    listInfo { page pageSize totalCount hasMore }
  }
}
"""

# resolveAlerts returns a plain Boolean (confirmed from the public reference), not the resolved
# ids -- the tool below reports back the ids it was given once SuperOps confirms success.
ALERT_RESOLVE = """
mutation resolveAlerts($input: ResolveAlertInput) {
  resolveAlerts(input: $input)
}
"""

ScriptId = Annotated[str, Field(description="Script id.", min_length=1)]
AssetId = Annotated[str, Field(description="Asset id.", min_length=1)]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def superops_automation_scripts_list(
        scriptType: Annotated[str | None, Field(description='Only scripts of this type, e.g. "Powershell".')] = None,
        page: Page = None,
        pageSize: PageSize = None,
        max: Max = None,
        cursor: Cursor = None,
        offset: Offset = None,
    ) -> str:
        """List scripts available to run on a managed asset, optionally filtered by type."""

        async def action(client: SuperOpsClient) -> Any:
            number, size, start = locate(offset, *paging(page, pageSize, cursor, max))
            script_type = text(scriptType)
            if script_type:
                variables = {"input": {"scriptType": script_type, "listInfo": list_input(number, size)}}
                data = await client.query(SCRIPT_LIST_BY_TYPE, variables)
                result = data.get("getScriptListByType")
            else:
                data = await client.query(SCRIPT_LIST, {"input": list_input(number, size)})
                result = data.get("getScriptList")
            return page_result(result, "scripts", number, size, start)

        return await run_tool("superops_automation_scripts_list", client_factory, action)

    @mcp.tool(annotations=ANY_WRITE)
    async def superops_automation_scripts_run(scriptId: ScriptId, assetId: AssetId) -> str:
        """Run a script on a managed endpoint.

        Call only when the user explicitly asks for this script to run on this asset.
        """

        async def action(client: SuperOpsClient) -> Any:
            script_id, asset_id = text(scriptId), text(assetId)
            if not script_id or not asset_id:
                raise ValueError("scriptId and assetId must not be blank.")
            variables = {"input": {"scriptId": script_id, "assetId": asset_id}}
            data = await client.query(SCRIPT_RUN, variables, operation="mutation")
            return data.get("runScriptOnAsset")

        return await run_tool("superops_automation_scripts_run", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def superops_automation_alerts_list(
        assetId: Annotated[
            str | None, Field(description="Only alerts for this asset; omit for every alert in the tenant.")
        ] = None,
        status: Annotated[str | None, Field(description='Alert status, e.g. "Open" or "Resolved".')] = None,
        severity: Annotated[str | None, Field(description='Alert severity, e.g. "High", "Medium", "Low".')] = None,
        page: Page = None,
        pageSize: PageSize = None,
        max: Max = None,
        cursor: Cursor = None,
        offset: Offset = None,
    ) -> str:
        """List alerts: tenant-wide, or scoped to one asset.

        listInfo.hasMore is null (not false) on the last page.
        """

        async def action(client: SuperOpsClient) -> Any:
            number, size, start = locate(offset, *paging(page, pageSize, cursor, max))
            condition = join(
                "AND",
                [
                    clause("status", "includes", [text(status)]) if text(status) else None,
                    clause("severity", "includes", [text(severity)]) if text(severity) else None,
                ],
            )
            asset_id = text(assetId)
            if asset_id:
                variables = {"input": {"assetId": asset_id, "listInfo": list_input(number, size, condition)}}
                data = await client.query(ALERT_LIST_FOR_ASSET, variables)
                result = data.get("getAlertsForAsset")
            else:
                data = await client.query(ALERT_LIST, {"input": list_input(number, size, condition)})
                result = data.get("getAlertList")
            return page_result(result, "alerts", number, size, start)

        return await run_tool("superops_automation_alerts_list", client_factory, action)

    @mcp.tool(annotations=WRITES_IDEMPOTENT)
    async def superops_automation_alerts_resolve(
        alertIds: Annotated[list[str], Field(description="One or more alert ids to mark resolved.")],
    ) -> str:
        """Mark the given alert(s) resolved. Call only when the user explicitly asks.

        Resolving an alert that is already resolved is not an error.
        """

        async def action(client: SuperOpsClient) -> Any:
            ids = [stripped for i in alertIds if (stripped := text(i))]
            if not ids:
                raise ValueError("alertIds must contain at least one non-blank id.")
            data = await client.query(ALERT_RESOLVE, {"input": {"alertIds": ids}}, operation="mutation")
            if not data.get("resolveAlerts"):
                raise SuperOpsError(502, "SuperOps did not confirm the alert(s) were resolved.")
            return {"alertIds": ids, "resolved": True}

        return await run_tool("superops_automation_alerts_resolve", client_factory, action)
